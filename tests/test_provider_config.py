import json
from pathlib import Path
import httpx
import pytest

from support_content import seed_content
from fastapi.testclient import TestClient
from azusa_bot.app import create_app, backup_project
from azusa_bot.credential_store import CredentialStore, CredentialStoreError
from azusa_bot.provider_config import describe, save
from azusa_bot.providers import CloudProviders
from azusa_bot.secrets import Credentials, resolve_credentials
from azusa_bot.settings import Settings, read_settings, write_settings

KEY = 'synthetic-only-key-DO-NOT-ECHO'

@pytest.fixture
def root(tmp_path, monkeypatch):
    for name in ('DEEPSEEK_API_KEY','FISH_API_KEY','FISH_REFERENCE_ID','FISH_MODEL','AZUSA_COMPATIBLE_API_KEY','AZUSA_COMPATIBLE_BASE_URL'):
        monkeypatch.delenv(name,raising=False)
    root = tmp_path/'bot'
    seed_content(root)
    write_settings(root,Settings())
    return root


def test_save_keys_write_only_voice_override_and_keep_settings(root):
    settings=read_settings(root); settings.group_chat_enabled=True
    write_settings(root,settings)
    result=save(root,{'settings':{'fish_reference_id':'0123456789abcdef0123456789abcdef','fish_model':'new-model'},'secrets':{'deepseek_key':KEY,'fish_key':KEY}})
    assert KEY not in json.dumps(result)
    assert result['credentials']['fish']=={'configured':True,'source':'keychain'}
    assert read_settings(root).group_chat_enabled
    c,_=resolve_credentials(root,read_settings(root),Credentials(fish_reference='old-voice',fish_model='old-model'))
    assert c.fish_reference=='0123456789abcdef0123456789abcdef' and c.fish_model=='new-model'
    save(root,{'settings':{'deepseek_model':'another-model'}})
    assert CredentialStore(root).get('deepseek_key')==KEY
    assert KEY not in (root/'config/local.json').read_text()


def test_priority_and_clear_only_keychain(root,monkeypatch):
    save(root,{'secrets':{'deepseek_key':KEY}})
    monkeypatch.setenv('DEEPSEEK_API_KEY','synthetic-env-key')
    c,source=resolve_credentials(root,read_settings(root),Credentials(deepseek_key='synthetic-dsh-key'))
    assert c.deepseek_key=='synthetic-env-key' and source['deepseek']['source']=='environment'
    save(root,{'clear_secrets':['deepseek_key']})
    assert describe(root)['credentials']['deepseek']['source']=='environment'
    monkeypatch.delenv('DEEPSEEK_API_KEY')
    c,source=resolve_credentials(root,read_settings(root),Credentials(deepseek_key='synthetic-dsh-key'))
    assert c.deepseek_key=='synthetic-dsh-key' and source['deepseek']['source']=='dsh'


def test_compatible_endpoint_and_project_key_isolation(root,tmp_path):
    first='https://models.example/v1'; second='https://other.example/v1'
    save(root,{'settings':{'llm_base_url':first},'secrets':{'compatible_key':KEY,'deepseek_key':KEY}})
    assert describe(root)['credentials']['compatible']['configured']
    save(root,{'settings':{'llm_base_url':second}})
    assert not describe(root)['credentials']['compatible']['configured']
    assert not CredentialStore(tmp_path/'other').get('compatible_key',first)
    save(root,{'settings':{'llm_base_url':first}})
    assert describe(root)['credentials']['compatible']['configured']


@pytest.mark.parametrize('url',['http://example.com/v1','https://u:secret@example.com/v1','https://example.com?key=x','https://example.com/#secret','https://example.com/\n'])
def test_invalid_endpoint_rejected(root,url):
    with pytest.raises(ValueError):
        save(root,{'settings':{'llm_base_url':url}})


def test_keychain_failure_rollback_and_safe_errors(root,monkeypatch):
    real=CredentialStore.set
    def fail(self,name,value,base_url=''):
        if name=='fish_key':
            raise RuntimeError(KEY)
        real(self,name,value,base_url)
    monkeypatch.setattr(CredentialStore,'set',fail)
    with pytest.raises(CredentialStoreError) as error:
        save(root,{'settings':{'fish_reference_id':'new'},'secrets':{'deepseek_key':KEY,'fish_key':KEY}})
    assert KEY not in str(error.value)
    assert not CredentialStore(root).get('deepseek_key')
    assert read_settings(root).fish_reference_id==''


def test_admin_api_auth_no_echo_or_backup_credentials(root,caplog):
    app=create_app(root)
    token=(root/'data/admin.token').read_text()
    headers={'Authorization':'Bearer '+token}
    with TestClient(app) as client:
        assert client.get('/admin/providers').status_code==401
        response=client.put('/admin/providers',headers=headers,json={'secrets':{'fish_key':KEY},'settings':{'fish_reference_id':'new-voice'}})
        assert response.status_code==200 and KEY not in response.text
        assert KEY not in client.get('/admin/status',headers=headers).text
        response=client.put('/admin/providers',headers=headers,json={'secrets':{'unknown':KEY}})
        assert response.status_code==400 and KEY not in response.text
        response=client.put('/admin/settings',headers=headers,json={'fish_key':KEY})
        assert response.status_code in (400,422) and KEY not in response.text
        backup=backup_project(root,app.state.engine)
        assert all(KEY.encode() not in p.read_bytes() for p in backup.rglob('*') if p.is_file())
        assert KEY not in caplog.text
    app.state.engine.memory.close();app.state.engine.groups.close()


@pytest.mark.asyncio
async def test_compatible_requests_use_only_separate_key_and_omit_deepseek_extension(root,monkeypatch):
    requests=[]
    original=httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"text":"ok"}'}}]})
    monkeypatch.setattr('azusa_bot.providers.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    provider=CloudProviders(Credentials(deepseek_key='never-send-this',compatible_key=KEY),'selected-model','fish',root/'media',protocol='openai_compatible',base_url='https://models.example/v1')
    await provider.complete([{'role':'user','content':'test'}])
    assert requests[0].headers['Authorization']=='Bearer '+KEY
    assert str(requests[0].url)=='https://models.example/v1/chat/completions'
    body=json.loads(requests[0].content)
    assert 'thinking' not in body and body['model']=='selected-model'


def test_legacy_fish_model_preserved_until_explicit_model_selection(root,monkeypatch):
    legacy=lambda _:Credentials(fish_model='working-legacy-model',fish_reference='old-reference')
    monkeypatch.setattr('azusa_bot.secrets.load_credentials',legacy)
    assert describe(root)['settings']['fish_model']=='working-legacy-model'
    save(root,{'settings':{'fish_reference_id':'new-reference'},'secrets':{'fish_key':KEY}})
    assert describe(root)['effective']['fish_model']=='working-legacy-model'
    save(root,{'settings':{'fish_model':'chosen-model'}})
    assert describe(root)['effective']['fish_model']=='chosen-model'


def test_waiting_keychain_save_does_not_restore_disabled_qq(root,monkeypatch):
    import threading
    settings=read_settings(root); settings.onebot_enabled=True
    write_settings(root,settings)
    entered,release=threading.Event(),threading.Event()
    real=CredentialStore.get
    def waiting(self,name,base_url=''):
        if name=='fish_key' and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return real(self,name,base_url)
    monkeypatch.setattr(CredentialStore,'get',waiting)
    errors=[]
    def worker():
        try:
            save(root,{'settings':{'fish_reference_id':'new-voice'},'secrets':{'fish_key':KEY}})
        except Exception as error:
            errors.append(error)
    thread=threading.Thread(target=worker);thread.start()
    assert entered.wait(3)
    settings=read_settings(root);settings.onebot_enabled=False
    settings.onebot_allowed_groups=['12345']
    write_settings(root,settings)
    release.set();thread.join(3)
    assert not thread.is_alive() and not errors
    latest=read_settings(root)
    assert not latest.onebot_enabled and latest.onebot_allowed_groups==['12345']
    assert latest.fish_reference_id=='new-voice'


def test_context_budget_is_global_editable_and_preserves_other_settings(root):
    before=read_settings(root)
    before.onebot_allowed_groups=['12345']
    before.fish_reference_id='synthetic-voice'
    write_settings(root,before)
    assert describe(root)['settings']['context_window_tokens']==32768
    result=save(root,{'settings':{'context_window_tokens':128000}})
    assert result['settings']['context_window_tokens']==128000
    latest=read_settings(root)
    assert latest.context_window_tokens==128000
    assert latest.onebot_allowed_groups==['12345'] and latest.fish_reference_id=='synthetic-voice'


@pytest.mark.parametrize('value',[4095,1000001,32768.5,True,'32768',None])
def test_context_budget_rejects_invalid_values_without_partial_save(root,value):
    with pytest.raises(ValueError):
        save(root,{'settings':{'context_window_tokens':value,'fish_reference_id':'must-not-commit'}})
    assert read_settings(root).context_window_tokens==32768
    assert read_settings(root).fish_reference_id==''


def test_context_budget_provider_api_is_authenticated(root):
    app=create_app(root)
    headers={'Authorization':'Bearer '+(root/'data/admin.token').read_text()}
    with TestClient(app) as client:
        assert client.put('/admin/providers',json={'settings':{'context_window_tokens':8192}}).status_code==401
        response=client.put('/admin/providers',headers=headers,json={'settings':{'context_window_tokens':8192}})
        assert response.status_code==200 and response.json()['settings']['context_window_tokens']==8192
        assert client.get('/admin/providers',headers=headers).json()['settings']['context_window_tokens']==8192
    app.state.engine.close()
