import json
from pathlib import Path
import pytest

from support_content import seed_content
from azusa_bot.core import Engine,Incoming
from azusa_bot.providers import ProviderError
from azusa_bot.settings import Settings,write_settings
from azusa_bot.diagnostics import Diagnostics


def test_only_fixed_codes_and_no_content_are_persisted(tmp_path):
    d=Diagnostics(tmp_path/'status.sqlite3')
    secret='synthetic-key-or-user-content-must-not-appear'
    d.record('llm',secret);d.record('llm','deepseek_http_503')
    value=d.status()
    assert secret not in json.dumps(value)
    assert value['services'][0]['failures']==2
    assert '上游' in value['services'][0]['message']
    d.record('llm','ok')
    assert d.status()['services'][0]['failures']==0
    assert secret.encode() not in (tmp_path/'status.sqlite3').read_bytes()


@pytest.mark.asyncio
async def test_direct_cloud_failure_silent_backoff_and_recovery(tmp_path,monkeypatch):
    root=tmp_path/'bot';seed_content(root)
    settings=Settings(provider_mode='cloud',qq_enabled=True,app_id='app',allowed_dm_users=['u'],min_interval_seconds=0)
    write_settings(root,settings);engine=Engine(root)
    class Provider:
        calls=0
        fail=True
        async def complete(self,messages,**kwargs):
            self.calls+=1
            if self.fail:raise ProviderError('deepseek_http_503')
            return {'text':'接着聊吧。','mode':'text'}
    p=Provider();engine.provider_override=p
    def event(i,text='你好'):
        return Incoming(app_id='app',kind='dm',scope_id='u',user_id='u',event_id=str(i),text=text)
    result=await engine.handle(event(1))
    assert result['mode']=='none' and result['text']=='' and result['reason']=='cloud_failure'
    assert engine.diagnostics.status()['services'][0]['code']=='deepseek_http_503'
    result=await engine.handle(event(2))
    assert result['reason']=='cloud_backoff' and p.calls==1
    assert (await engine.handle(event(3,'/身份')))['mode']=='text'
    assert p.calls==1
    p.fail=False;engine._cloud_retry_after=0
    assert (await engine.handle(event(4)))['text']=='接着聊吧。'
    assert engine.diagnostics.status()['services'][0]['state']=='ok'
    assert engine._cloud_failure_count==0
    engine.memory.close();engine.groups.close()


def test_context_cancellation_is_neutral_not_a_provider_failure(tmp_path):
    from azusa_bot.diagnostics import Diagnostics
    d=Diagnostics(tmp_path/'state.sqlite3')
    d.record('llm','privacy_context_changed')
    result=d.status()['services'][0]
    assert result['state']=='cancelled' and result['failures']==0
    assert '不表示隐私泄露' in result['message']
    d.record('llm','deepseek_http_503');d.record('llm','privacy_context_changed')
    assert d.status()['services'][0]['failures']==1
    assert d.status()['events'][0]['state']=='cancelled'
