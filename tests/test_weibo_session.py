"""Mobile-login diagnostics and default activity context, without live secrets."""
import asyncio
import json

import httpx
import pytest

from azusa_bot import role_activity,lore
from azusa_bot.bilibili import BilibiliSubscriptions
from azusa_bot.subscription_credentials import update_credentials
from azusa_bot.subscription_sources import SubscriptionError,WEIBO_CONFIG_URL
from azusa_bot.weibo_session import verify,status
from test_role_activity import row,enable
from test_knowledge_admin import library


@pytest.fixture
def session(tmp_path):
    now=[1700000000.0];calls=[];reply=[httpx.Response(200,json={'data':{'login':True}})]
    def handler(request):
        calls.append(request)
        assert str(request.url)==WEIBO_CONFIG_URL
        assert 'iPhone' in request.headers['user-agent']
        assert request.headers['MWeibo-Pwa']=='1'
        assert request.headers['Cookie'].startswith('SUB=synthetic')
        return reply[0]
    update_credentials(tmp_path,{'weibo_cookie':'SUB=synthetic-original'})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BilibiliSubscriptions(tmp_path,client=client,clock=lambda:now[0]),now,calls,reply


@pytest.mark.parametrize('response,state,error',[
    (httpx.Response(200,json={'data':{'login':True}}),'valid',''),
    (httpx.Response(200,json={'data':{'login':False}}),'invalid','weibo_session_not_logged_in'),
    (httpx.Response(200,json={'data':{'login':1}}),'error','weibo_session_response_unknown'),
    (httpx.Response(432,text='private response'),'error','weibo_login_required'),
    (httpx.Response(200,text='<html>private challenge</html>'),'error','weibo_response_not_json'),
    (httpx.Response(429,headers={'Retry-After':'7200'}),'error','weibo_rate_limited'),
])
async def test_session_diagnostic_is_precise_cached_and_does_not_query_feed(session,response,state,error):
    service,now,calls,reply=session;reply[0]=response
    assert (await status(service))['state']=='unknown' and not calls
    result=await verify(service)
    assert result['state']==state and result['error']==error and len(calls)==1
    assert 'synthetic' not in json.dumps(result) and 'fingerprint' not in result and 'private' not in json.dumps(result)
    again=await verify(service);assert again['cached'] and len(calls)==1
    restart=BilibiliSubscriptions(service.root,client=service.client,clock=service.clock)
    assert (await status(restart))['state']==state and len(calls)==1
    if state=='invalid':
        with pytest.raises(SubscriptionError,match='weibo_session_invalid'):await restart.sources.weibo('456')
        assert len(calls)==1
    if response.status_code==429:assert result['retry_at']==now[0]+7200


async def test_new_credentials_do_not_inherit_old_validation_or_reset_feed_cooldown(session):
    service,now,calls,reply=session
    reply[0]=httpx.Response(200,json={'data':{'login':False}})
    await verify(service)
    service.sources.cooldown.claim('weibo:456')
    limited=service.sources.cooldown.failed('weibo:456',SubscriptionError('weibo_rate_limited',retry_after_seconds=7200))
    update_credentials(service.root,{'weibo_cookie':'SUB=synthetic-new'})
    assert (await status(service))['state']=='unknown'
    assert (await verify(service))['cached'] and len(calls)==1
    now[0]+=61;reply[0]=httpx.Response(200,json={'data':{'login':True}})
    assert (await verify(service))['state']=='valid' and len(calls)==2
    with pytest.raises(SubscriptionError,match='weibo_cooldown') as error:await service.sources.weibo('456')
    assert error.value.retry_at==limited.retry_at and len(calls)==2
    update_credentials(service.root,{'clear':['weibo_cookie']})
    assert (await verify(service))['state']=='unconfigured' and len(calls)==2


async def test_duplicate_checks_coalesce_while_request_in_flight(session):
    service,now,calls,reply=session;entered=asyncio.Event();release=asyncio.Event()
    original=service.sources.request
    async def blocked(*args):
        entered.set();await release.wait();return await original(*args)
    service.sources.request=blocked
    task=asyncio.create_task(verify(service));await entered.wait()
    other=await verify(service);assert other['state']=='checking' and other['cached'] and not calls
    release.set();assert (await task)['state']=='valid' and len(calls)==1


async def test_unknown_recheck_does_not_release_confirmed_invalid_session(session):
    service,now,calls,reply=session
    reply[0]=httpx.Response(200,json={'data':{'login':False}})
    assert (await verify(service))['feed_blocked']
    now[0]+=61;reply[0]=httpx.Response(432)
    result=await verify(service)
    assert result['state']=='error' and result['feed_blocked']
    with pytest.raises(SubscriptionError,match='weibo_session_invalid'):
        await service.sources.weibo('456')
    assert len(calls)==2
    now[0]+=61;reply[0]=httpx.Response(200,json={'data':{'login':True}})
    assert not (await verify(service))['feed_blocked']


def test_recent_activity_preview_matches_default_selection_and_excludes_expired(tmp_path,monkeypatch):
    now=1700000000.0;monkeypatch.setattr(role_activity.time,'time',lambda:now)
    r=row(now);enable(tmp_path,[r]);role_activity.sync(tmp_path,[r],now)
    preview=role_activity.describe(tmp_path,[r])['context_preview']
    actual=[r for r in lore.for_prompt(tmp_path,'完全无关的企鹅') if r.get('selection_reason')=='recent_activity']
    assert preview['automatic'] and len(preview['items'])==1
    assert [(r['doc_id'],r['text']) for r in preview['items']]==[(r['doc_id'],r['text']) for r in actual]
    monkeypatch.setattr(role_activity.time,'time',lambda:now+86400)
    assert role_activity.describe(tmp_path,[r])['context_preview']['items']==[]
    assert lore.list_documents(tmp_path)['total']==1


def test_session_admin_auth_and_origin_are_enforced(library):
    root,app,client,headers=library
    for method,path in [('get','/admin/subscriptions/weibo-session'),('post','/admin/subscriptions/weibo-session/verify')]:
        assert getattr(client,method)(path).status_code==401
        assert getattr(client,method)(path,headers=headers|{'Origin':'https://foreign.invalid'}).status_code==403
        good=getattr(client,method)(path,headers=headers)
        assert good.status_code==200 and good.json()['state']=='unconfigured'
    assert client.post('/admin/subscriptions/weibo-session/verify',headers=headers|{'X-XZBot-Profile':'wrong'}).status_code==409
