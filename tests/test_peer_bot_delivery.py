"""Peer drafts remain revocable until the bridge authorizes each real send."""

import pytest

from azusa_bot import conversation_config, segmented_delivery
from azusa_bot.core import Ledger
from azusa_bot.profiles import ProfileEngineManager
from test_peer_bot_interaction import HUMAN, event, manager, pair, scope


async def prepare(pair):
    (left, _), (provider, _), _ = pair
    provider.say("周六可以去河边散步。带上水和一把折叠伞。")
    reply = await left.handle(event(1, "有什么周末活动建议？", mentioned=True))
    assert reply["mode"] == "text", reply
    assert left.authorize_reply(reply["reply_id"])
    return left, provider, reply


@pytest.mark.parametrize("sender", [HUMAN, "20002"])
async def test_human_priority_revokes_peer_draft_but_unrelated_peer_activity_does_not(pair, sender):
    left, provider, reply = await prepare(pair)
    provider.answer = {"participate": False, "confidence": 0, "text": ""}
    await left.handle(event(2, "另一个话题里大家正在聊晚饭。", sender=sender))
    assert left.authorize_reply(reply["reply_id"]) is (sender != HUMAN)
    assert left.groups.peer_budget(scope())["used"] == (0 if sender == HUMAN else 1)


async def test_missing_peer_draft_still_fails_closed_with_prepared_ledger_row(pair):
    left, _, reply = await prepare(pair)
    with left.ledger.db() as db:
        row = db.execute("SELECT status,peer_bot FROM deliveries WHERE reply_id=?",(reply["reply_id"],)).fetchone()
    assert dict(row) == {"status":"prepared", "peer_bot":1}
    left.current._group_pending.clear()
    assert not left.authorize_reply(reply["reply_id"])
    restarted = ProfileEngineManager(left.root)
    try:
        assert not restarted.authorize_reply(reply["reply_id"])
    finally:
        restarted.close()
        restarted.groups.close()


async def test_peer_segments_recheck_activity_between_sends(pair):
    (left, _), _, _ = pair
    config = conversation_config.read(left.content_root)
    config["segmented_reply"].update(enabled=True, max_segments=3, interval_seconds=0)
    conversation_config.save(left.content_root,config,conversation_config.revision(conversation_config.read(left.content_root)))
    left, provider, reply = await prepare(pair)
    rid = reply["reply_id"]
    assert len(reply["segments"]) == 2
    assert segmented_delivery.authorize(left,rid,0)
    segmented_delivery.acknowledge(left,rid,0,"sent","101")
    assert left.authorize_reply(rid)
    provider.answer = {"participate": False, "confidence": 0, "text": ""}
    await left.handle(event(2,"先别安排了，我们改天再讨论。",sender=HUMAN))
    assert not segmented_delivery.authorize(left,rid,1)
    segmented_delivery.settle(left.current,rid,abort=True)
    assistant = [row["text"] for row in left.groups.snapshot(scope()) if row["role"] == "assistant"]
    assert assistant == [reply["segments"][0]]


def test_existing_delivery_ledger_migrates_peer_guard_flag(tmp_path):
    import sqlite3
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE deliveries (event_key TEXT PRIMARY KEY,reply_id TEXT UNIQUE,scope TEXT,user_id TEXT,status TEXT,mode TEXT,at REAL)")
        db.execute("INSERT INTO deliveries VALUES ('old','old','scope','user','prepared','text',0)")
    ledger = Ledger(path)
    with ledger.db() as db:
        assert db.execute("SELECT peer_bot FROM deliveries WHERE reply_id='old'").fetchone()[0] == 0
