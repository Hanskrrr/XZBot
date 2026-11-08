import asyncio
import concurrent.futures
from pathlib import Path

import pytest

from azusa_bot.group_chat import GroupConversation


class Clock:
    def __init__(self):
        self.now = 100000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def group(tmp_path):
    clock = Clock()
    value = GroupConversation(tmp_path / "group_state.sqlite3",clock=clock)
    yield value,clock
    value.close()


def test_context_dedup_stable_ids_and_group_isolation(group):
    state,clock = group
    first = state.add("g1","u1","今晚玩什么","m1",display_name="同名")
    assert state.add("g1","u1","重复事件","m1") is None
    state.add("g1","u2","我想打游戏","m2",display_name="同名")
    state.add("g2","u1","另一个群的内容","m1")
    assert [row["user_id"] for row in state.snapshot("g1")] == ["u1","u2"]
    assert "另一个群" not in str(state.snapshot("g1"))
    assert state.familiarity("g1","u1")["message_count"] == 1
    assert not state.is_current("g1",first)
    # Persistent metadata dedup survives process restart, without replaying text.
    restarted = GroupConversation(state.path,clock=clock)
    assert restarted.snapshot("g1") == []
    assert restarted.add("g1","u1","重播同一事件","m1") is None
    assert restarted.familiarity("g1","u1")["message_count"] == 1


def test_dynamic_snapshot_limits_and_hard_scope_member_message_bounds(tmp_path):
    clock = Clock()
    state = GroupConversation(tmp_path/"state.sqlite3",max_scopes=2,max_messages=3,max_members=2,ttl_seconds=7200,clock=clock)
    state.add("g1","u1","第一条","m1")
    clock.advance(100)
    state.add("g1","u2","第二条","m2")
    clock.advance(100)
    state.add("g1","u3","第三条","m3")
    assert [r["user_id"] for r in state.snapshot("g1")] == ["u2","u3"]
    assert len(state.snapshot("g1",ttl_seconds=50)) == 1
    assert len(state.snapshot("g1",ttl_seconds=1000,limit=1)) == 1
    assert len(state.snapshot("g1",ttl_seconds=1000)) == 2
    for n in range(4):
        state.add("g2","u1",f"文字{n}",f"g2-{n}")
    assert len(state.snapshot("g2")) == 3
    state.add("g3","u1","最新群","g3")
    assert state.snapshot("g1") == []
    assert state.get_stats()["context_scopes"] == 2
    assert state.get_stats()["familiar_scopes"] <= 2
    clock.advance(7200)
    assert state.get_stats()["context_messages"] == 0


def test_local_filter_omits_commands_keys_controls_and_oversize_text(group):
    state,_ = group
    bad = ["/记忆 开启","!管理指令","" + ("sk-" + "example" * 5) + "","Bearer abcdefghijklmnopqrstuvwx",
           "api_key=private-example-value","密码：example-password","长"*2001,"含有\x00控制字符",""]
    for n,text in enumerate(bad):
        assert state.add("g1","u1",text,f"bad-{n}") is None
    assert state.snapshot("g1") == [] and state.familiarity("g1","u1") is None
    assert state.add("g1","u1","API key 是什么？","safe") is not None


def test_no_chat_text_persists_and_familiarity_stays_low_risk(group,tmp_path):
    state,_ = group
    text = "THIS_MESSAGE_MUST_NEVER_BE_PERSISTED_378596"
    state.add("g1","u1",text,"private-message-id",display_name="展示名，不是权限")
    state.add("g1","bot","机器人已发送的内容","reply",role="assistant")
    meta = state.familiarity("g1","u1")
    assert set(meta) == {"scope","user_id","display_name","first_seen","last_seen","message_count"}
    assert state.familiarity("g1","bot") is None
    backup = tmp_path/"backup.sqlite3"
    state.backup(backup)
    for path in (state.path,backup):
        assert text.encode() not in path.read_bytes()
        assert b"private-message-id" not in path.read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
    assert "展示名，不是权限" in str(meta)


def test_persistent_optout_removes_context_and_metadata_and_blocks_recollection(group):
    state,clock = group
    state.add("g1","u1","应该删除","m1",display_name="昵称")
    state.add("g1","u2","保留别人的","m2")
    state.add("g2","u1","另一个群","m3")
    state.set_optout("g1","u1",True)
    assert state.familiarity("g1","u1") is None
    assert [x["user_id"] for x in state.snapshot("g1")] == ["u2"]
    assert state.add("g1","u1","不应继续旁听","m4") is None
    restarted = GroupConversation(state.path,clock=clock)
    assert restarted.opted_out("g1","u1")
    assert restarted.add("g1","u1","重启仍拒绝","m5") is None
    assert restarted.familiarity("g2","u1") is not None
    restarted.set_optout("g1","u1",False)
    assert restarted.add("g1","u1","主动重新开启","m6") is not None
    restarted.remove_user("g1","u1")
    assert restarted.familiarity("g1","u1") is None


def test_optout_capacity_fails_closed_without_dropping_prior_refusals(tmp_path):
    state = GroupConversation(tmp_path/"state.sqlite3",max_scopes=1,max_members=1)
    state.set_optout("g1","u1")
    state.set_optout("g2","u2")
    assert state.opted_out("g1","u1") and state.opted_out("g2","u2")
    assert state.add("g3","u3","不得继续采集","m3") is None
    assert state.get_stats()["optouts"] == 1
    assert state.get_stats()["collection_disabled"] == 1


def test_reserved_and_sent_replies_share_durable_hour_day_and_cooldown_limits(group):
    state,clock = group
    budget = dict(cooldown_seconds=10,hour_limit=2,day_limit=3)
    first = state.reserve("g1",**budget)
    assert first and state.budget_status("g1",**budget)["reason"] == "cooldown"
    assert state.snapshot("g1") == []  # Reservation never invents sent bot text.
    assert state.mark_sent(first)
    assert state.budget_status("g1",**budget)["last_activity"]["status"] == "sent"
    clock.advance(11)
    assert state.reserve("g1",**budget)
    assert state.budget_status("g1",**budget)["reason"] == "hour_budget"
    state.clear_scope("g1")
    restarted = GroupConversation(state.path,clock=clock)
    assert restarted.reserve("g1",**budget) is None
    clock.advance(3600)
    assert restarted.reserve("g1",**budget)
    assert restarted.budget_status("g1",**budget)["reason"] == "day_budget"
    clock.advance(86400)
    assert restarted.budget_status("g1",**budget)["allowed"]


def test_budget_check_does_not_spend_and_clear_or_forget_cannot_refund(group):
    state,_ = group
    settings = dict(cooldown_seconds=0,hour_limit=1,day_limit=1)
    for _ in range(3):
        assert state.budget_status("g1",**settings)["allowed"]
    assert state.get_stats()["reply_reservations"] == 0
    state.add("g1","u1","临时话题","m1")
    state.reserve("g1",**settings)
    state.remove_user("g1","u1")
    state.clear_scope("g1")
    assert state.reserve("g1",**settings) is None
    assert state.reserve("g2",**settings)


def test_atomic_reservation_across_instances_prevents_parallel_sends(group):
    state,clock = group
    second = GroupConversation(state.path,clock=clock)
    config = dict(cooldown_seconds=30,hour_limit=5,day_limit=20)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        slots = list(pool.map(lambda n: (state if n%2 else second).reserve("g1",**config),range(20)))
    assert sum(slot is not None for slot in slots) == 1
    assert state.budget_status("g1",**config)["day_used"] == 1


@pytest.mark.asyncio
async def test_debounce_requires_latest_event_and_scope_lock_serializes(group):
    state,_ = group
    older = state.add("g1","u1","第一条","m1")
    waiting = asyncio.create_task(state.debounced("g1",older,0.02))
    latest = state.add("g1","u2","第二条","m2")
    assert await waiting is False
    assert await state.debounced("g1",latest,0) is True
    active = 0
    maximum = 0

    async def decision():
        nonlocal active,maximum
        async with state.lock("g1"):
            active += 1
            maximum = max(maximum,active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(decision() for _ in range(10)))
    assert maximum == 1
    state.clear_scope("g1")
    assert not state.is_current("g1",latest)


def test_context_and_budget_capacity_do_not_reset_existing_reservations(tmp_path):
    clock = Clock()
    state = GroupConversation(tmp_path/"state.sqlite3",max_scopes=1,clock=clock)
    budget = dict(cooldown_seconds=0,hour_limit=2,day_limit=2)
    assert state.reserve("g1",**budget)
    state.add("g1","u1","旧群","m1")
    state.add("g2","u1","新群","m2")
    assert state.snapshot("g1") == []
    assert state.budget_status("g2",**budget)["reason"] == "scope_capacity"
    assert state.budget_status("g1",**budget)["day_used"] == 1
