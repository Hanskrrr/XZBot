"""The idle transport is text-only, online-only, authorized and single-send."""
import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest
pytest.importorskip('astrbot')
from test_onebot_bridge import make_bridge, Bot, SELF, GROUP

ITEM={'id':'a'*32,'claim_token':'b'*32,'self_id':str(SELF),'target_id':str(GROUP),'text':'合成冷场短句'}

def fixture(monkeypatch,*,online=True,allowed=True,fail=False,receipt=True,after_authorize=None,ack_fail=False):
    calls=[];claimed=False
    def handler(request):
        nonlocal claimed
        body=json.loads(request.content);calls.append((request.url.path,body))
        if request.url.path.endswith('/claim'):
            result={'messages':[] if claimed else [ITEM]};claimed=True
        elif request.url.path.endswith('/authorize'):
            if after_authorize:after_authorize(bridge)
            result={'allowed':allowed}
        else:
            if ack_fail:return httpx.Response(500)
            result={'acknowledged':True}
        return httpx.Response(200,json=result)
    bridge=make_bridge(monkeypatch,handler,group_chat_enabled=True,group_idle_enabled=True,group_idle_groups=[str(GROUP)])
    bot=Bot(fail=fail,receipt=receipt);bot._wsr_api_clients={str(SELF):object()} if online else {}
    bridge.context=SimpleNamespace(get_platform_inst=lambda _:SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp'),get_client=lambda:bot))
    return bridge,bot,calls

def run(bridge):
    async def go():
        try:
            await bridge._idle_tick();await bridge._idle_tick()
        finally:await bridge.terminate()
    asyncio.run(go())

def test_single_idle_text_send_with_receipt(monkeypatch):
    bridge,bot,calls=fixture(monkeypatch);run(bridge)
    assert len(bot.calls)==1
    assert bot.calls[0][1]['message']==[{'type':'text','data':{'text':ITEM['text']}}]
    acks=[b for p,b in calls if p.endswith('/ack')]
    assert len(acks)==1 and acks[0]['status']=='sent' and acks[0]['message_id']=='-999'

@pytest.mark.parametrize('online,allowed',[(False,True),(True,False)])
def test_offline_or_revoked_never_sends(monkeypatch,online,allowed):
    bridge,bot,calls=fixture(monkeypatch,online=online,allowed=allowed);run(bridge)
    assert not bot.calls
    if not online:assert not calls

@pytest.mark.parametrize('change',[
    lambda b:b.config.update(enabled=False),lambda b:b.config.update(group_chat_enabled=False),
    lambda b:b.config.update(group_idle_enabled=False),lambda b:b.config.update(group_idle_groups=[]),
    lambda b:b.config.update(allowed_groups=[]),lambda b:b.config.update(self_id='999999')])
def test_policy_rechecked_immediately_before_send(monkeypatch,change):
    bridge,bot,calls=fixture(monkeypatch,after_authorize=change);run(bridge)
    assert not bot.calls
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['failed']

@pytest.mark.parametrize('fail,receipt,ack_fail',[(True,True,False),(False,False,False),(False,True,True)])
def test_send_uncertainty_or_ack_failure_never_retries(monkeypatch,fail,receipt,ack_fail):
    bridge,bot,calls=fixture(monkeypatch,fail=fail,receipt=receipt,ack_fail=ack_fail);run(bridge)
    assert len(bot.calls)==1
    assert len([b for p,b in calls if p.endswith('/ack')])==1

def test_idle_task_stops_on_bridge_termination(monkeypatch):
    bridge,_,calls=fixture(monkeypatch,online=False)
    async def go():
        await bridge.initialize();await asyncio.sleep(0)
        task=bridge._idle_task
        await bridge.terminate();assert task.done()
    asyncio.run(go());assert not calls
