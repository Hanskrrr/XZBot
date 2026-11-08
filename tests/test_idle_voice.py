"""Both proactive paths share Profile TTS policy and cancellation, no cloud/QQ."""
import asyncio
from pathlib import Path
import pytest
from azusa_bot.settings import write_settings
from azusa_bot.providers import ProviderError
from test_group_idle import rig,ready,SELF,SCOPE,activity
from test_group_voice import Provider


@pytest.mark.parametrize('manual',[False,True])
@pytest.mark.parametrize('outcome',['voice','disabled','failure','revoked'])
async def test_idle_voice_modes_and_revocation(rig,manual,outcome):
    e,s,clock,_=rig;s.voice_enabled=outcome!='disabled';s.voice_authorized=True
    s.fish_model_override=True;s.fish_reference_id='synthetic-voice';s.fish_model='synthetic-tts'
    write_settings(e.root,s);p=Provider(e.root);e.provider_override=p
    if outcome=='failure':p.error='fish_http_503'
    if outcome=='revoked':p.entered=asyncio.Event();p.release=asyncio.Event()
    if manual:
        e.idle.queue_manual(SCOPE,'d'*32);call=e.idle.claim_manual(SELF)
    else:
        await ready(rig);call=e.idle.claim(SELF)
    if outcome=='revoked':
        task=asyncio.create_task(call);await asyncio.wait_for(p.entered.wait(),3)
        await e.handle(activity(99));p.release.set();assert await task==[]
        assert all(not path.exists() for path in p.generated)
        return
    item,=await call
    assert item['mode']==('voice' if outcome=='voice' else 'text')
    assert len(p.speech)==(0 if outcome=='disabled' else 1)
    if outcome=='voice':
        path=Path(item['media_path']);assert path.is_file()
        assert e.idle.authorize(item['id'],item['claim_token'],SELF)
        assert e.idle.ack(item['id'],item['claim_token'],SELF,'sent','123')['acknowledged']
        assert not path.exists()
        traces=e.trajectories.list()['items'];tts=next(x for x in traces if x['kind']=='tts')
        source=next(x for x in traces if x['kind']==('group_idle_manual' if manual else 'group_idle'))
        assert tts['related_id']==source['id']
    else:assert 'media_path' not in item and '语音已发送' not in item['text']
