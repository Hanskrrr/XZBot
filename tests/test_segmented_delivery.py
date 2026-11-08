"""Actual receipt ordering, interrupted plans and account/role invalidation."""
from types import SimpleNamespace
import pytest
from azusa_bot import segmented_delivery as delivery
from azusa_bot.trajectory_admin import attach_delivery
from test_conversation_execution import configure, Model
from test_web_search import rig, event

TEXT='第一句已经完整说完了。第二句是另外一个意思！第三句是最后的补充。'


async def prepare(rig):
    engine,_,_,_=rig
    configure(engine,segmented_reply={'enabled':True,'max_segments':3,'interval_seconds':0})
    engine.provider_override=Model([{'text':TEXT,'mode':'text'}])
    reply=await engine.handle(event(text='来聊聊'))
    assert len(reply['segments'])==3
    def authorized(reply_id):
        with engine.ledger.db() as db:
            row=db.execute('SELECT status FROM deliveries WHERE reply_id=?',(reply_id,)).fetchone()
        return row and row[0]=='prepared' and engine.is_active()
    return engine,reply,SimpleNamespace(current=engine,authorize_reply=authorized)


@pytest.mark.asyncio
async def test_segment_claims_are_ordered_and_receipts_commit_once(rig):
    engine,reply,manager=await prepare(rig);rid=reply['reply_id']
    assert not delivery.authorize(manager,rid,1)
    for i in range(3):
        assert delivery.authorize(manager,rid,i)
        assert not delivery.authorize(manager,rid,i)
        delivery.acknowledge(manager,rid,i,'sent',str(900+i))
        delivery.acknowledge(manager,rid,i,'sent',str(900+i))
    delivery.settle(engine,rid,abort=True)
    records=engine.groups.snapshot(reply['scope'])
    assert [x['text'] for x in records if x['role']=='assistant']==reply['segments']
    rows=engine.dialogue.get(reply['scope'],'20001',ttl_seconds=300)
    assert rows[-1]['text']=='\n'.join(reply['segments'])
    trace=attach_delivery(engine,engine.trajectories.list()['items'])[0]
    assert trace['delivery']['segmented']['sent']==3 and trace['delivery']['state']=='sent'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['uncertain','cancelled','privacy'])
async def test_partial_history_contains_only_confirmed_prefix(rig,failure):
    engine,reply,manager=await prepare(rig);rid=reply['reply_id']
    assert delivery.authorize(manager,rid,0)
    delivery.acknowledge(manager,rid,0,'sent','900')
    if failure=='uncertain':
        assert delivery.authorize(manager,rid,1)
        delivery.acknowledge(manager,rid,1,'uncertain')
    elif failure=='privacy':engine.clear_group_context(reply['scope'])
    delivery.settle(engine,rid,abort=True)
    assert not delivery.authorize(manager,rid,1)
    rows=engine.dialogue.get(reply['scope'],'20001',ttl_seconds=300)
    if failure=='privacy':assert rows==[]
    else:assert rows[-1]['text']==reply['segments'][0] and reply['segments'][1] not in str(rows)
    trace=attach_delivery(engine,engine.trajectories.list()['items'])[0]
    assert trace['delivery']['state']=='partial' and trace['delivery']['segmented']['sent']==1


@pytest.mark.asyncio
async def test_restart_cannot_replay_a_prepared_plan(rig):
    engine,reply,manager=await prepare(rig)
    engine._group_pending.clear()
    assert not delivery.authorize(manager,reply['reply_id'],0)
    delivery.settle(engine,reply['reply_id'],abort=True)
    assert engine.dialogue.get(reply['scope'],'20001',ttl_seconds=300)==[]
