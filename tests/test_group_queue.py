"""Synthetic concurrency and timer checks. No live model or QQ traffic."""
import asyncio
import json
import time

import pytest
from azusa_bot.group_queue import GroupQueue,QueueStopped
from context_helpers import context
from test_group_engine import rig,event,SCOPE,configure


@pytest.mark.asyncio
async def test_fifo_keeps_different_members_merges_pending_fragments_and_isolates_groups():
    queue=GroupQueue();locks={'g':asyncio.Lock(),'h':asyncio.Lock()}
    entered=asyncio.Event();release=asyncio.Event();seen=[]
    async def work(group,user,number,replaceable=True):
        try:
            async with queue.turn(group,user,number,replaceable=replaceable,lock=locks[group]):
                seen.append(number)
                if number=='first':entered.set();await release.wait()
                return 'done'
        except QueueStopped as exc:return str(exc)
    first=asyncio.create_task(work('g','a','first'));await entered.wait()
    second=asyncio.create_task(work('g','b','b1'));await asyncio.sleep(0)
    third=asyncio.create_task(work('g','c','c1',False));await asyncio.sleep(0)
    fourth=asyncio.create_task(work('g','b','b2'));await asyncio.sleep(0)
    assert await second=='queue_merged'
    assert await work('h','d','other-group')=='done'
    snapshot=await asyncio.to_thread(queue.snapshot)
    assert snapshot['groups'][0]['waiting']==2
    release.set();assert await asyncio.gather(first,third,fourth)==['done']*3
    assert seen==['first','other-group','c1','b2']
    assert queue.rooms=={}


@pytest.mark.asyncio
async def test_explicit_questions_do_not_merge_and_queue_is_bounded():
    queue=GroupQueue(per_group=1);lock=asyncio.Lock();release=asyncio.Event();entered=asyncio.Event()
    async def work(number):
        try:
            async with queue.turn('g','same',number,replaceable=False,lock=lock):
                if number=='1':entered.set();await release.wait()
                return 'done'
        except QueueStopped as exc:return str(exc)
    first=asyncio.create_task(work('1'));await entered.wait()
    second=asyncio.create_task(work('2'));await asyncio.sleep(0)
    assert await work('3')=='queue_full'
    assert not second.done()
    release.set();assert await asyncio.gather(first,second)==['done','done']


@pytest.mark.asyncio
async def test_deadline_includes_shared_lock_and_cancel_does_not_jam_room():
    queue=GroupQueue(max_wait_seconds=.03);lock=asyncio.Lock();await lock.acquire()
    with pytest.raises(QueueStopped,match='queue_expired'):
        async with queue.turn('g','u','1',replaceable=True,lock=lock):assert False
    assert queue.rooms=={}
    async def work():
        async with queue.turn('g','u','2',replaceable=True,lock=lock):pass
    pending=asyncio.create_task(work());await asyncio.sleep(0);pending.cancel()
    with pytest.raises(asyncio.CancelledError):await pending
    lock.release()
    async with queue.turn('g','u','3',replaceable=True,lock=lock):pass
    assert queue.rooms=={}


@pytest.mark.asyncio
async def test_privacy_withdrawal_clears_waiters_without_unlocking_active_generation():
    queue=GroupQueue();lock=asyncio.Lock();entered=asyncio.Event();release=asyncio.Event()
    async def work(number):
        try:
            async with queue.turn('g',number,number,replaceable=False,lock=lock):
                if number=='1':entered.set();await release.wait()
                return 'done'
        except QueueStopped as exc:return str(exc)
    first=asyncio.create_task(work('1'));await entered.wait()
    second=asyncio.create_task(work('2'));await asyncio.sleep(0)
    await asyncio.to_thread(queue.clear,'g')
    assert await second=='queue_context_changed' and lock.locked()
    release.set();await first


@pytest.mark.asyncio
async def test_engine_queues_different_members_and_rebuilds_context_without_inline_summary(rig):
    engine,settings,provider,clock=rig
    configure(engine,settings,auto_summary_every=4)
    engine.memory.set_consent(SCOPE,'20001',True)
    for n in range(3):engine.memory.add_history(SCOPE,'20001','user',f'先前发言 {n}',f'seed-{n}')
    provider.entered,provider.release=asyncio.Event(),asyncio.Event()
    first=asyncio.create_task(engine.handle(event('第一个问题',event_id='1',mentioned=True,unprompted=False)))
    await asyncio.wait_for(provider.entered.wait(),1)
    second=asyncio.create_task(engine.handle(event('第二个问题',event_id='2',user_id='20002',mentioned=True,unprompted=False)))
    await asyncio.sleep(.01)
    assert engine.group_queue.snapshot()['groups'][0]['waiting']==1
    with engine.ledger.db() as db:
        assert db.execute("SELECT status FROM deliveries WHERE source_event_id='2'").fetchone()[0]=='queued'
    assert len(provider.calls)==1 and not provider.calls[0]['summary']
    provider.release.set();reply=await first
    assert not second.done()
    engine.acknowledge(reply['reply_id'],'sent','90001')
    await second
    assert len(provider.calls)==2 and all(not c['summary'] for c in provider.calls)
    newest=context(provider.calls[-1]['messages'])
    assert newest['current_message']['text']=='第二个问题'
    assert any(r['text']=='第一个问题' for r in newest['recent_conversation'])
    assert any(r['role']=='assistant' and r['message_id']=='90001' for r in newest['recent_conversation'])
    assert engine.group_queue.rooms=={}


@pytest.mark.asyncio
async def test_timer_updates_member_background_after_max_wait_without_any_new_message(rig):
    engine,settings,provider,clock=rig
    configure(engine,settings,auto_summary_every=12,auto_summary_max_wait_minutes=5)
    engine.memory.set_consent(SCOPE,'20001',True)
    source=engine.memory.add_history(SCOPE,'20001','user','我叫小明，喜欢猫。','first')
    with engine.memory._db:engine.memory._db.execute('UPDATE history SET created_at=? WHERE id=?',(time.time()-301,source))
    provider.summary_answer={'memories':[],'background':{'text':'小明喜欢猫。','source_ids':[source]},'interactions':[]}
    result=await engine.summarize_due()
    assert result['status']=='saved' and result['saved']==1
    from azusa_bot.member_notes import background
    assert background(engine.memory,SCOPE,'20001')['text']=='小明喜欢猫。'
    assert len(provider.calls)==1 and provider.calls[0]['summary']
    assert (await engine.summarize_due())['status']=='idle' and len(provider.calls)==1
