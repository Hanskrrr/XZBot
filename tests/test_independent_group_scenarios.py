"""Independent deployments reason only over messages they actually received."""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import sqlite3
import time

from pydantic import ValidationError
import pytest

from context_helpers import context as wire_context
from support_content import seed_content
from azusa_bot.bot_control import BotPowerController
from azusa_bot.core import Engine, Incoming
from azusa_bot.providers import MockProviders
from azusa_bot.settings import Settings, read_settings, write_settings


LEFT, RIGHT, HUMAN, OTHER, GROUP = "20001", "20002", "90001", "90002", "30001"


def test_peer_identity_configuration_defaults_are_independent_and_round_trip(tmp_path):
    first, second = Settings(), Settings()
    first.onebot_peer_bot_ids.append(RIGHT)
    assert second.onebot_peer_bot_ids == []
    configured = Settings(onebot_self_id=LEFT, onebot_peer_bot_ids=[RIGHT, "30003"])
    write_settings(tmp_path, configured)
    assert read_settings(tmp_path).onebot_peer_bot_ids == [RIGHT, "30003"]
    assert (tmp_path / "config/local.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("identity", ["0", "00123", "-123", "123 456", "OpenID_abc", "1" * 21, 123, True,
                                      "123\n", "sk_SYNTHETIC_SECRET"])
def test_peer_identity_requires_canonical_qq_ids(identity):
    with pytest.raises(ValidationError):
        Settings(onebot_peer_bot_ids=[identity])


def test_peer_identity_rejects_duplicates_self_and_unbounded_lists():
    for data in ({"onebot_peer_bot_ids": [RIGHT, RIGHT]},
                 {"onebot_self_id": LEFT, "onebot_peer_bot_ids": [LEFT]},
                 {"onebot_peer_bot_ids": [str(n) for n in range(1, 102)]}):
        with pytest.raises(ValidationError):
            Settings(**data)
    assert len(Settings(onebot_peer_bot_ids=[str(n) for n in range(1, 101)]).onebot_peer_bot_ids) == 100


@pytest.mark.parametrize("native,wording,expected", [
    ([LEFT], "请解释这个图案。", "self"),
    ([OTHER], "请解释这个图案。", "others"),
    ([], "@20001 [CQ:at,qq=20001] 你觉得呢？", "unknown"),
])
async def test_offline_mock_plan_uses_local_native_facts_not_textual_at(native, wording, expected):
    request = {"task": "group_plan", "self_id": LEFT,
        "current_message": {"message_id": "90", "user_id": HUMAN, "text": wording},
        "local_relation": {"target": "unknown", "addressee_ids": [], "thread_anchor": "90",
            "evidence_message_ids": ["90"], "facts": {"mentioned_user_ids": native, "quoted_author_id": None}}}
    answer = await MockProviders().complete([
        {"role": "system", "content": "离线规划演练"},
        {"role": "user", "content": "合成输入\n" + json.dumps(request, ensure_ascii=False)}])
    plan = answer["participation_plan"]
    assert plan["target"] == expected
    assert plan["action"] == ("reply" if expected == "self" else "observe")
    assert plan["evidence_message_ids"] == ["90"]
    assert "text" not in answer


@pytest.mark.parametrize("code", ["conversation_plan_changed", "conversation_plan_invalid",
    "conversation_plan_unavailable", "conversation_observed", "peer_round_limit"])
def test_local_conversation_stops_have_bounded_diagnostics(code):
    from azusa_bot.delivery_errors import safe_delivery_code, delivery_explanation, classify_send_error
    from azusa_bot.diagnostics import safe_code, explanation
    assert safe_code(code) == safe_delivery_code(code) == classify_send_error(RuntimeError(code)) == code
    assert explanation(code) == delivery_explanation(code)
    assert "QQ 接口返回错误码" not in explanation(code)


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds=20):
        self.now += seconds


class ScenarioProvider:
    """Plan decisions and generated speech are separately scripted and recorded."""
    def __init__(self, self_id):
        self.self_id = self_id
        self.calls, self.plan_calls, self.reply_calls = [], [], []
        self.plans, self.replies = deque(), deque()
        self.entered = self.release = None

    def plan(self, action="reply", *, addressees=None, response_to=None, evidence=None, anchor=None):
        def answer(data):
            current = data["current_message"]["message_id"]
            identities = [self.self_id] if addressees is None else addressees
            return {"confidence":.99, "votes":{key:.99 for key in ("relevance","understanding","contribution","timing")}, "participation_plan": {
                "action": action, "addressee_ids": identities,
                "target": "self" if self.self_id in identities else "others" if identities else "group" if action == "reply" else "unknown",
                "response_to_message_id": response_to or current,
                "evidence_message_ids": evidence or [current], "thread_anchor": anchor or current,
                "reason": "synthetic_relation",
            }}
        self.plans.append(answer)

    def say(self, text):
        self.replies.append({"text": text, "mode": "text", "participate": True,
                             "confidence": .99, "continuation": True,
                             "votes": {key: .99 for key in ("relevance", "understanding", "contribution", "timing")}})

    async def complete(self, messages, *, summary=False):
        assert not summary, "Scenario messages must not trigger implicit memory extraction"
        messages = deepcopy(messages)
        self.calls.append(messages)
        data = wire_context(messages)
        if data.get("task") == "group_plan":
            self.plan_calls.append(messages)
            assert self.plans, "Unexpected relationship-planning call: script its decision explicitly"
            return self.plans.popleft()(data)
        self.reply_calls.append(messages)
        assert self.replies, "Unexpected speech generation: no response was authorized in this scenario"
        answer = deepcopy(self.replies.popleft())
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        return answer


@dataclass
class Participant:
    engine: Engine
    provider: ScenarioProvider
    qq: str

    @property
    def root(self):
        return self.engine.root

    @property
    def scope(self):
        return f"onebot:{self.qq}:group:{GROUP}"

    async def receive(self, number, text, *, sender=HUMAN, name="小周", mentions=(), reply_to=None, quote=None, **changes):
        labels = {LEFT: "甲角色", RIGHT: "乙角色", HUMAN: "小周", OTHER: "小林"}
        event = Incoming(**(dict(transport="onebot", app_id=self.qq, kind="group", scope_id=GROUP,
            user_id=sender, event_id=str(number), text=text, display_name=name,
            mentioned_user_ids=list(mentions), unprompted=self.qq not in mentions,
            mentioned_members=[{"user_id": member, "display_name": labels[member]} for member in mentions if member in labels],
            reply_to_id=reply_to, quoted_message=quote) | changes))
        return await self.engine.handle(event)

    def acknowledge(self, response, message_id):
        assert response["mode"] == "text", response
        self.engine.acknowledge(response["reply_id"], "sent", str(message_id))


@pytest.fixture
def independent_pair(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("azusa_bot.core.time", SimpleNamespace(
        time=clock, monotonic=clock, strftime=time.strftime, gmtime=time.gmtime))
    members = []
    for directory, qq, peer, name in (("a", LEFT, RIGHT, "甲角色"), ("b", RIGHT, LEFT, "乙角色")):
        root = tmp_path / directory
        seed_content(root)
        write_settings(root, Settings(transport="onebot", onebot_enabled=True, onebot_self_id=qq,
            onebot_allowed_groups=[GROUP], onebot_peer_bot_ids=[peer], min_interval_seconds=0,
            group_chat_enabled=True, group_chat_debounce_seconds=0, group_chat_min_messages=1,
            group_chat_cooldown_seconds=15, group_chat_decision_interval_seconds=5,
            group_chat_hourly_limit=60, group_chat_daily_limit=300))
        BotPowerController(root, lambda: None)._write("running")
        engine = Engine(root)
        engine.address_names_loader = lambda own=name: [own]
        engine.groups.clock = clock
        provider = ScenarioProvider(qq)
        engine.provider_override = provider
        members.append(Participant(engine, provider, qq))
    yield (*members, clock)
    for member in members:
        member.engine.memory.close()
        member.engine.groups.close()


def test_plan_trajectory_keeps_only_routing_metadata_and_redacts_on_erasure(independent_pair):
    from azusa_bot.trajectories import references, safe_participation_plan
    left, _, _ = independent_pair
    plan = {"action": "reply", "target": "self", "addressee_ids": [LEFT],
        "message_id": "91", "response_to_message_id": "91", "thread_anchor": "original-event",
        "evidence_message_ids": ["91"], "reason": "SYNTHETIC_PRIVATE_REASON",
        "scratchpad": "SYNTHETIC_UNREQUESTED_ANALYSIS"}
    safe = safe_participation_plan(plan)
    assert safe_participation_plan(safe) == safe
    store = left.engine.trajectories
    number = store.begin("group_plan", "group_observation", left.scope, HUMAN, "mock", "mock",
        dict(references(), participation_plan=plan), request_text="合成群聊问题")
    store.called(number)
    store.finish(number, result={"participation_plan": plan})
    saved = store.list(kind="group_plan")["items"][0]
    assert saved["participation_plan"] == safe
    with left.engine.memory._lock:
        raw = left.engine.memory._db.execute("SELECT refs,result FROM trajectories WHERE id=?", (number,)).fetchone()
    assert all("SYNTHETIC_PRIVATE_REASON" not in value and "SYNTHETIC_UNREQUESTED_ANALYSIS" not in value for value in raw)
    store.erase_previews(scope=left.scope)
    assert store.list(kind="group_plan")["items"][0]["participation_plan"] is None


async def test_explicit_self_mention_uses_only_own_root_and_no_planning(independent_pair, monkeypatch):
    left, right, _ = independent_pair
    left.provider.say("这是甲角色对明确呼叫的回答。")
    original, original_connect = Path.open, sqlite3.connect
    reads = []

    def own_reads(path, *args, **kwargs):
        reads.append(path)
        assert not path.is_relative_to(right.root), "The left deployment read its neighbour's files"
        return original(path, *args, **kwargs)

    def registry_forbidden(*args, **kwargs):
        pytest.fail("Group semantics must not inspect a manager-wide account registry")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "open", own_reads)
        def own_database(database, *args, **kwargs):
            assert not Path(database).is_relative_to(right.root), "The left deployment opened its neighbour's database"
            return original_connect(database, *args, **kwargs)
        guarded.setattr(sqlite3, "connect", own_database)
        for module in ("azusa_bot.accounts", "azusa_bot.core"):
            for name in ("managed_qq_ids", "managed_bot_names"):
                guarded.setattr(f"{module}.{name}", registry_forbidden, raising=False)
        result = await left.receive(1, "甲角色，解释一下这个图案。", mentions=[LEFT])
    assert result["mode"] == "text", result
    assert reads
    assert not left.provider.plan_calls
    assert len(left.provider.reply_calls) == 1
    assert right.provider.calls == []
    assert right.engine.groups.snapshot(right.scope) == []
    assert set(left.root.parent.iterdir()) == {left.root, right.root}


async def test_own_configured_name_is_a_direct_call_without_native_mention(independent_pair):
    left, right, _ = independent_pair
    left.provider.say("甲角色会回答自己配置的称呼。")
    result = await left.receive(2, "甲角色，你推荐哪本书？")
    assert result["mode"] == "text", result
    assert not left.provider.plan_calls
    assert len(left.provider.reply_calls) == 1
    assert right.provider.calls == []


async def test_native_self_mention_with_plain_prose_is_not_dropped(independent_pair):
    left, _, _ = independent_pair
    left.provider.plan()
    left.provider.say("可以从图案的重复部分开始看。")
    result = await left.receive(3, "解释一下这个图案。", mentions=[LEFT])
    assert result["mode"] == "text", result
    assert len(left.provider.reply_calls) == 1
    assert wire_context(left.provider.reply_calls[0])["current_message"]["mentioned"] is True


async def test_pure_native_self_mention_uses_fast_reply(independent_pair):
    left, _, _ = independent_pair
    left.provider.say("在，怎么啦？")
    result = await left.receive(4, "", mentions=[LEFT])
    assert result["mode"] == "text", result
    assert not left.provider.plan_calls
    assert len(left.provider.reply_calls) == 1
    assert wire_context(left.provider.reply_calls[0])["current_message"]["mention_only"] is True


async def test_unknown_you_can_be_observed_without_generating_a_question(independent_pair):
    left, _, _ = independent_pair
    left.provider.plan("observe", addressees=[])
    result = await left.receive(5, "你觉得呢？")
    assert result["mode"] == "none", result
    assert len(left.provider.plan_calls) == 1
    assert not left.provider.reply_calls


def quote(clock, *, author=LEFT, message_id="701", text="你可以问问小林。"):
    return dict(message_id=message_id, user_id=author, kind="group", scope_id=GROUP,
                text=text, sent_at=int(clock()))


@pytest.mark.parametrize("to_bot", [True, False])
async def test_quote_self_and_mention_human_is_resolved_semantically(independent_pair, to_bot):
    left, _, clock = independent_pair
    left.provider.plan("reply" if to_bot else "observe", addressees=[LEFT if to_bot else OTHER])
    if to_bot:
        left.provider.say("我是在说小林的那张照片。")
    wording = "他昨天说的是这件事吗？" if to_bot else "小林，你自己解释一下这句话吧。"
    result = await left.receive(10, wording, mentions=[OTHER], reply_to="701", quote=quote(clock),
        mentioned_members=[{"user_id": OTHER, "display_name": "小林"}])
    assert len(left.provider.plan_calls) == 1
    assert len(left.provider.reply_calls) == int(to_bot)
    assert result["mode"] == ("text" if to_bot else "none"), result
    current = wire_context(left.provider.plan_calls[0])["current_message"]
    assert current["sender"]["user_id"] == HUMAN
    assert current["quoted_message"]["user_id"] == LEFT
    assert current["mentions"][0]["user_id"] == OTHER
    if to_bot:
        plan = wire_context(left.provider.reply_calls[0])["participation_plan"]
        assert plan["action"] == "reply" and plan["addressee_ids"] == [LEFT]


async def test_human_turns_to_another_human_then_you_does_not_reopen_old_bot_thread(independent_pair):
    left, _, clock = independent_pair
    left.provider.say("我建议你周六下午去图书馆。")
    initial = await left.receive(20, "甲角色，周末去哪里？", mentions=[LEFT])
    left.acknowledge(initial, 120)
    clock.advance()
    redirected = await left.receive(21, "小林，你明天有空吗？", mentions=[OTHER],
        mentioned_members=[{"user_id": OTHER, "display_name": "小林"}])
    assert redirected["mode"] == "none", redirected
    clock.advance()
    left.provider.plan("observe", addressees=[OTHER], evidence=["21", "22"], anchor="21")
    continuation = await left.receive(22, "你几点能到？")
    assert continuation["mode"] == "none", continuation
    assert len(left.provider.reply_calls) == 1
    if left.provider.plan_calls:
        recent = wire_context(left.provider.plan_calls[-1])["recent_conversation"]
        redirected_row = next(row for row in recent if row["message_id"] == "21")
        assert redirected_row["mentions"][0]["user_id"] == OTHER


async def test_interleaved_people_keep_separate_conversation_evidence(independent_pair):
    left, _, clock = independent_pair
    left.provider.say("入门摄影可以先用你现有的相机。")
    initial = await left.receive(30, "甲角色，我想学摄影。", mentions=[LEFT])
    left.acknowledge(initial, 130)
    clock.advance()
    side = await left.receive(31, "小周，晚饭要不要吃面？", sender=OTHER, name="小林", mentions=[HUMAN])
    assert side["mode"] == "none", side
    clock.advance()
    left.provider.plan("reply", evidence=["30", "130", "32"], anchor="30")
    left.provider.say("镜头先选轻便的定焦，拍熟悉的日常场景。")
    continued = await left.receive(32, "你刚才说相机先不用换，那镜头呢？")
    assert continued["mode"] == "text", continued
    latest = wire_context(left.provider.reply_calls[-1])
    assert latest["current_message"]["user_id"] == HUMAN
    assert {"30", "130", "31"} <= {row["message_id"] for row in latest["recent_conversation"]}
    assert latest["participation_plan"]["thread_anchor"] == "30"


async def test_other_name_is_known_only_after_local_group_observation(independent_pair):
    left, right, clock = independent_pair
    left.provider.plan("observe", addressees=[])
    observed = await left.receive(40, "我带了一本新书。", sender=OTHER, name="小林")
    assert observed["mode"] == "none", observed
    clock.advance()
    addressed = await left.receive(41, "小林，你买的是什么书？")
    assert addressed["mode"] == "none", addressed
    assert not left.provider.reply_calls
    assert right.engine.groups.snapshot(right.scope) == []
    assert not right.provider.calls


async def test_peer_text_affects_deduplication_only_after_actual_local_receipt(independent_pair):
    left, right, clock = independent_pair
    shared_text = "周六上午可以去河边散步，沿着步道走到旧码头。"
    right.provider.say(shared_text)
    peer_reply = await right.receive(50, "乙角色，给一个周末建议。", mentions=[RIGHT])
    right.acknowledge(peer_reply, 150)

    # Generating and acknowledging B's output cannot place it in A's context.
    left.provider.say(shared_text)
    before_receipt = await left.receive(51, "甲角色，给一个周末建议。", mentions=[LEFT])
    assert before_receipt["mode"] == "text", before_receipt
    assert not any(row.get("user_id") == RIGHT for row in wire_context(left.provider.reply_calls[-1]).get("recent_conversation", []))
    # Leave A's draft unacknowledged: it must not become its own spoken history.
    clock.advance()
    left.provider.plan("observe", addressees=[HUMAN])
    peer_seen = await left.receive(150, shared_text, sender=RIGHT, name="乙角色", mentions=[HUMAN])
    assert peer_seen["mode"] == "none", peer_seen
    clock.advance()
    left.provider.say(shared_text)
    after_receipt = await left.receive(52, "甲角色，你有什么周末建议？", mentions=[LEFT])
    assert after_receipt["mode"] == "none", after_receipt
    assert after_receipt["reason"] == "repetitive_reply"
    rows = wire_context(left.provider.reply_calls[-1])["recent_conversation"]
    peer = next(row for row in rows if row["message_id"] == "150")
    assert peer["user_id"] == RIGHT and peer["is_bot"] is True
    assert not any(row["role"] == "assistant" for row in rows)


async def test_opposite_receipt_orders_are_local_and_create_no_shared_state(independent_pair):
    left, right, clock = independent_pair
    left.provider.say("甲角色解释第一个问题。")
    right.provider.say("乙角色解释第二个问题。")
    first = await left.receive(60, "甲角色，先解释第一个问题。", mentions=[LEFT])
    second = await right.receive(61, "乙角色，解释第二个问题。", mentions=[RIGHT])
    assert first["mode"] == second["mode"] == "text"
    for participant, unseen in ((left, "61"), (right, "60")):
        rows = wire_context(participant.provider.reply_calls[0]).get("recent_conversation", [])
        assert unseen not in {row["message_id"] for row in rows}
    clock.advance()
    assert (await left.receive(61, "乙角色，解释第二个问题。", mentions=[RIGHT]))["mode"] == "none"
    assert (await right.receive(60, "甲角色，先解释第一个问题。", mentions=[LEFT]))["mode"] == "none"
    assert [row["message_id"] for row in left.engine.groups.snapshot(left.scope)] == ["60", "61"]
    assert [row["message_id"] for row in right.engine.groups.snapshot(right.scope)] == ["61", "60"]
    assert len(left.provider.reply_calls) == len(right.provider.reply_calls) == 1
    assert set(left.root.parent.iterdir()) == {left.root, right.root}


async def test_unreceived_switch_cannot_cancel_a_draft_but_local_switch_and_late_ack_can(independent_pair):
    left, right, clock = independent_pair
    left.provider.say("这是甲角色尚未确认发出的旧回答。")
    old = await left.receive(70, "甲角色，谈谈这个方案。", mentions=[LEFT])
    assert left.engine.target_reply_current(old["reply_id"])
    clock.advance()
    right.provider.say("乙角色现在回答新的问题。")
    newer = await right.receive(71, "乙角色，现在我问你。", mentions=[RIGHT])
    assert newer["mode"] == "text", newer
    assert left.engine.target_reply_current(old["reply_id"]), "A cannot know about an event delivered only to B"

    local_switch = await left.receive(71, "乙角色，现在我问你。", mentions=[RIGHT])
    assert local_switch["mode"] == "none", local_switch
    assert not left.engine.target_reply_current(old["reply_id"])
    left.engine.acknowledge(old["reply_id"], "sent", "170")
    clock.advance()
    left.provider.plan("observe", addressees=[RIGHT], evidence=["71", "72"], anchor="71")
    next_turn = await left.receive(72, "你准备怎么回答？")
    assert next_turn["mode"] == "none", next_turn
    assert len(left.provider.reply_calls) == 1
    assert left.engine._active_thread(left.scope, HUMAN, left.engine.settings()) is None


async def test_local_human_redirect_invalidates_a_reply_already_in_generation(independent_pair):
    left, _, clock = independent_pair
    left.provider.say("这句旧回答不该在对方转向之后发送。")
    left.provider.entered, left.provider.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(left.receive(80, "甲角色，帮我看看。", mentions=[LEFT]))
    try:
        await asyncio.wait_for(left.provider.entered.wait(), 2)
        clock.advance()
        changed = await left.receive(81, "小林，我现在问你。", mentions=[OTHER])
        assert changed["mode"] == "none", changed
        left.provider.release.set()
        old = await asyncio.wait_for(pending, 2)
        assert old["mode"] == "none", old
        if old.get("reply_id"):
            assert not left.engine.target_reply_current(old["reply_id"])
    finally:
        left.provider.release.set()
        if not pending.done():
            await pending
