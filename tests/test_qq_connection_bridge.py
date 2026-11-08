"""Read-only OneBot status reports; never send QQ or trigger a login."""
import asyncio
import json
from types import SimpleNamespace

import pytest
pytest.importorskip('astrbot')
from test_onebot_bridge import make_bridge, SELF


@pytest.mark.parametrize('transport,online,raises',[(True,True,False),(True,False,False),(True,None,False),(True,True,True),(False,True,False)])
def test_bridge_reports_status_without_claiming_or_sending(monkeypatch,transport,online,raises):
    reports=[];queried=[]
    import httpx
    def handler(request):
        assert request.url.path=='/bridge/connection-status'
        reports.append(json.loads(request.content));return httpx.Response(200,json={'recorded':True})
    bridge=make_bridge(monkeypatch,handler)
    async def status(**kwargs):
        queried.append(kwargs)
        if raises:raise RuntimeError('private error')
        return {'online':online}
    bot=SimpleNamespace(_wsr_api_clients={str(SELF):object()} if transport else {},get_status=status)
    bridge.context=SimpleNamespace(get_platform_inst=lambda _:SimpleNamespace(meta=lambda:SimpleNamespace(name='aiocqhttp'),get_client=lambda:bot))
    async def run():
        try:await bridge._status_tick()
        finally:await bridge.terminate()
    asyncio.run(run())
    assert queried==([{'self_id':SELF}] if transport else [])
    assert reports==[{'self_id':str(SELF),'connected':transport,'online':online if transport and not raises else None}]
