"""Pinned AstrBot notification transport; fake HTTP and QQ only."""
import asyncio,json,base64
from types import SimpleNamespace
import pytest
pytest.importorskip('astrbot')
from test_onebot_bridge import make_bridge, Bot, SELF, USER, GROUP
import httpx

ITEM={'id':'a'*32,'claim_token':'b'*32,'self_id':str(SELF),'transport':'onebot','kind':'group',
      'target_id':str(GROUP),'room_id':'123','session':'123:test','text':'开播啦 [CQ:at,qq=all]\nhttps://live.bilibili.com/123'}


def fixture(monkeypatch,*,bot=None,allowed=True,after_authorize=None,ack_fail=False,notification=None,online=True):
    calls=[];claimed=False
    def handler(request):
        nonlocal claimed
        body=json.loads(request.content);calls.append((request.url.path,body))
        if request.url.path.endswith('/claim'):
            result={'notifications':[] if claimed else [notification or ITEM]};claimed=True
        elif request.url.path.endswith('/authorize'):
            if after_authorize:after_authorize(bridge)
            result={'allowed':allowed}
        else:
            if ack_fail:return httpx.Response(500)
            result={'acknowledged':True}
        return httpx.Response(200,json=result)
    bridge=make_bridge(monkeypatch,handler)
    bot=bot or Bot();bot._wsr_api_clients={str(SELF):object()} if online else {}
    bridge.context=SimpleNamespace(get_platform_inst=lambda _:SimpleNamespace(
        meta=lambda:SimpleNamespace(name='aiocqhttp'),get_client=lambda:bot))
    return bridge,bot,calls


def run(bridge):
    async def go():
        try:
            await bridge._notification_tick();await bridge._notification_tick()
        finally:await bridge.terminate()
    asyncio.run(go())


def test_single_plain_notification_and_receipt(monkeypatch):
    bridge,bot,calls=fixture(monkeypatch)
    run(bridge)
    assert len(bot.calls)==1
    assert bot.calls[0][1]['message']==[{'type':'text','data':{'text':ITEM['text']}}]
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['sent']


@pytest.mark.parametrize('kind,target,method',[('dm',USER,'private'),('group',GROUP,'group')])
def test_notification_routes(monkeypatch,kind,target,method):
    bridge,bot,_=fixture(monkeypatch,notification=ITEM|{'kind':kind,'target_id':str(target)})
    run(bridge);assert bot.calls[0][0]==method


@pytest.mark.parametrize('change',[
    lambda b:b.config.update(enabled=False),
    lambda b:b.config.update(allowed_groups=[]),
    lambda b:b.config.update(self_id='999999'),
])
def test_policy_rechecked_after_authorization(monkeypatch,change):
    bridge,bot,calls=fixture(monkeypatch,after_authorize=change)
    run(bridge);assert not bot.calls
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['failed']


def test_cancelled_subscription_not_sent(monkeypatch):
    bridge,bot,calls=fixture(monkeypatch,allowed=False)
    run(bridge);assert not bot.calls


def test_offline_transport_never_claims(monkeypatch):
    bridge,bot,calls=fixture(monkeypatch,online=False)
    run(bridge);assert not calls and not bot.calls


@pytest.mark.parametrize('bot',[Bot(fail=True),Bot(receipt=False)])
def test_uncertain_send_has_no_retry(monkeypatch,bot):
    bridge,bot,calls=fixture(monkeypatch,bot=bot)
    run(bridge);assert len(bot.calls)==1
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['uncertain']


def test_lost_ack_does_not_resend(monkeypatch):
    bridge,bot,_=fixture(monkeypatch,ack_fail=True)
    run(bridge);assert len(bot.calls)==1


def test_background_task_stops_on_terminate(monkeypatch):
    bridge,_,_=fixture(monkeypatch,online=False)
    async def go():
        await bridge.initialize()
        await asyncio.sleep(0)
        task=bridge._notification_task
        await bridge.terminate()
        assert task.done()
    asyncio.run(go())


def test_card_and_text_use_one_send_and_one_receipt(monkeypatch, tmp_path):
    from PIL import Image
    card = tmp_path / ('c' * 32 + '.png')
    Image.new('RGB', (8, 8), 'blue').save(card)
    monkeypatch.setenv('AZUSA_MEDIA_ROOT', str(tmp_path))
    bridge,bot,calls=fixture(monkeypatch,notification=ITEM|{'image_file':str(card)})
    run(bridge)
    assert len(bot.calls)==1
    parts=bot.calls[0][1]['message']
    assert [part['type'] for part in parts]==['text','image']
    assert parts[0]['data']['text']==ITEM['text']
    assert base64.b64decode(parts[1]['data']['file'].removeprefix('base64://'))==card.read_bytes()
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['sent']


def test_readable_dynamic_long_card_stays_in_one_qq_message(monkeypatch, tmp_path):
    from pathlib import Path
    from PIL import Image
    from azusa_bot.subscription_cards import Cards
    cards=Cards(tmp_path)
    async def synthetic_image(url):
        return Image.new('RGB',(812,1100),'#7f9b91') if url else None
    monkeypatch.setattr(cards,'_image',synthetic_image)
    info={'type':'bilibili_dynamic','name':'合成动态作者','card':{'blocks':[
        {'text':'这是一段用于验证长图发送的合成正文。\n'*18,
         'images':['https://i0.hdslb.com/one.png','https://i0.hdslb.com/two.png']},
        {'label':'转发原文 · 合成作者','text':'这段原文也在同一张图片中。','images':[]}]}}
    path,error=asyncio.run(cards.render(info,'合成通知\nhttps://t.bilibili.com/123'))
    assert error=='' and Path(path).stat().st_size<=8*1024*1024
    with Image.open(path) as image:
        assert image.width==960 and image.height>2500
    monkeypatch.setenv('AZUSA_MEDIA_ROOT',str(cards.media))
    bridge,bot,calls=fixture(monkeypatch,notification=ITEM|{'image_file':path})
    run(bridge)
    assert len(bot.calls)==1
    parts=bot.calls[0][1]['message']
    assert [part['type'] for part in parts]==['text','image']
    assert base64.b64decode(parts[1]['data']['file'].removeprefix('base64://'))==Path(path).read_bytes()
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['sent']


@pytest.mark.parametrize('bad',['missing','outside','symlink','invalid_png','remote'])
def test_invalid_image_falls_back_before_single_send(monkeypatch,tmp_path,bad):
    media=tmp_path/'media';media.mkdir()
    card=media/('c'*32+'.png')
    if bad=='outside':
        card=tmp_path/('d'*32+'.png');card.write_bytes(b'\x89PNG\r\n\x1a\nprivate')
    elif bad=='symlink':
        other=tmp_path/'private';other.write_bytes(b'\x89PNG\r\n\x1a\nprivate');card.symlink_to(other)
    elif bad=='invalid_png':card.write_bytes(b'not-an-image')
    value='https://example.invalid/private.png' if bad=='remote' else str(card)
    monkeypatch.setenv('AZUSA_MEDIA_ROOT',str(media))
    bridge,bot,calls=fixture(monkeypatch,notification=ITEM|{'image_file':value})
    run(bridge)
    assert len(bot.calls)==1
    assert bot.calls[0][1]['message']==[{'type':'text','data':{'text':ITEM['text']}}]


def test_image_send_uncertain_never_adds_text_retry(monkeypatch,tmp_path):
    from PIL import Image
    card=tmp_path/('c'*32+'.png');Image.new('RGB',(8,8)).save(card)
    monkeypatch.setenv('AZUSA_MEDIA_ROOT',str(tmp_path))
    bridge,bot,calls=fixture(monkeypatch,bot=Bot(fail=True),notification=ITEM|{'image_file':str(card)})
    run(bridge)
    assert len(bot.calls)==1 and len(bot.calls[0][1]['message'])==2
    assert [b['status'] for p,b in calls if p.endswith('/ack')]==['uncertain']
