"""The existing poller supplies one source snapshot for multiple destinations."""
import json
from azusa_bot.role_activity import observations
from test_subscriptions import rig,body,tick,dynamic


async def test_successful_parsed_snapshot_cached_once_per_source(rig):
    service,now,feed,_,calls,_=rig
    await service.add(body());await service.add(body(target_id='20002'))
    before=len(calls);await tick(rig)
    rows=observations(service)
    assert len(rows)==1 and rows[0]['key']=='bilibili_dynamic:456' and rows[0]['enabled']
    cached=json.loads(rows[0]['payload'])['events']
    assert len(cached)==1 and cached[0]['session']=='100'
    assert cached[0]['card']['blocks'][0]['text']=='合成动态内容'
    assert len(calls)-before==1
    assert service.fetch_claim('10001')==[]
