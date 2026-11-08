"""Run this on all three native CI runners, with disposable local state only."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest
from fastapi.testclient import TestClient
from azusa_bot import host
from azusa_bot.settings import private_write,token,Settings,write_settings
from azusa_bot.app import create_app


def test_private_file_open_and_permissions(tmp_path):
    path=tmp_path/'credential';private_write(path,'synthetic-only')
    with os.fdopen(host.safe_open(path,os.O_RDONLY)) as f:
        assert host.private_info(os.fstat(f.fileno()),f.fileno())
        assert f.read()=='synthetic-only'
    assert host.private_path(path)
    assert host.python_at(tmp_path).name==('python.exe' if os.name=='nt' else 'python')


def test_native_lock_excludes_another_process(tmp_path):
    path=tmp_path/'lock'
    with path.open('a') as f:
        host.file_locks.flock(f,host.file_locks.LOCK_EX|host.file_locks.LOCK_NB)
        code="from azusa_bot.host import file_locks as f; import sys; h=open(sys.argv[1],'a');\ntry:f.flock(h,f.LOCK_EX|f.LOCK_NB)\nexcept BlockingIOError:sys.exit(23)\n"
        result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True)
        assert result.returncode==23,result.stderr.decode(errors='replace')
        host.file_locks.flock(f,host.file_locks.LOCK_UN)


def test_native_directory_link(tmp_path):
    target=tmp_path/'target';target.mkdir();(target/'x').write_text('example')
    host.link_directory(tmp_path/'linked',target)
    assert (tmp_path/'linked/x').read_text()=='example'


def test_admin_startup_and_external_configuration_archive(tmp_path):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    from release_initialize import initialize
    (tmp_path/'config').mkdir();(tmp_path/'examples').mkdir()
    private_write(tmp_path/'config/example.json',Settings().model_dump_json())
    private_write(tmp_path/'examples/persona.md','# Test role')
    initialize(tmp_path)
    private_write(tmp_path/'config/runtime.json',json.dumps({'mode':'external'}))
    app=create_app(tmp_path)
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+token(tmp_path,'admin')}
        assert client.get('/admin/status').status_code==401
        assert client.get('/admin/status',headers=headers).status_code==200
        assert client.get('/admin/runtime/external',headers=headers).json()['external']
        response=client.get('/admin/runtime/external/config',headers=headers)
        assert response.status_code==200
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            assert set(z.namelist())=={'onebot11.json','webui.json','napcat.json'}
            connection=json.loads(z.read('onebot11.json'))['network']['websocketClients'][0]
            assert connection['url']=='ws://127.0.0.1:6199/ws'
            assert connection['token']==token(tmp_path,'onebot')
    app.state.engine.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows native credential store')
def test_windows_credential_vault_roundtrip(tmp_path):
    from azusa_bot.credential_store import CredentialStore
    store=CredentialStore(tmp_path)
    try:
        store.set('deepseek_key','synthetic-test-only')
        assert store.get('deepseek_key')=='synthetic-test-only'
        assert store.storage_kind()=='windows_credential_manager'
    finally:store.set('deepseek_key','')
