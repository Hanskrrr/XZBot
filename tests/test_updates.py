"""Release update boundaries with synthetic packages; no external network or real services."""
import hashlib
import json
from pathlib import Path
import platform
import zipfile
import pytest
from azusa_bot.updates import archive
from azusa_bot.updates.service import UpdateService, version, DEFAULTS
from test_app import http


def package(tmp_path, *, release_version='0.9.1', extra=None):
    info={'version':release_version,'platform':{'Darwin':'macos','Windows':'windows','Linux':'linux'}[platform.system()],
          'arch':{'AMD64':'x86_64','arm64':'aarch64'}.get(platform.machine(),platform.machine()),'commit':'a'*40}
    files={'README.md':b'new program','src/example.py':b'print("synthetic")',
           'scripts/install_release.py':b'pass\n','RELEASE-INFO.json':json.dumps(info).encode()}
    files.update(extra or {})
    files['RELEASE-FILES.json']=json.dumps({n:hashlib.sha256(v).hexdigest() for n,v in files.items()}).encode()
    target=tmp_path/('package-'+release_version+'.zip')
    with zipfile.ZipFile(target,'w') as z:
        for name,body in files.items():z.writestr('XZBot/'+name,body)
    return target,info|{'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'url':'https://downloads.example.test/'+target.name},files


def installed(root, files):
    for name,body in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
    for name in ('content/persona.md','config/local.json','data/history.sqlite3','accounts/private.txt'):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('PRIVATE SENTINEL')


def test_install_and_rollback_preserve_private_state_and_remove_obsolete_program(tmp_path):
    old,_,files=package(tmp_path,release_version='0.9.0',extra={'src/obsolete.py':b'old'})
    root=tmp_path/'installed 中文 space';installed(root,files)
    new,release,_=package(tmp_path);backup=tmp_path/'rollback'
    archive.install_files(root,new,release,backup)
    assert not (root/'src/obsolete.py').exists()
    for name in ('content/persona.md','config/local.json','data/history.sqlite3','accounts/private.txt'):
        assert (root/name).read_text()=='PRIVATE SENTINEL'
    archive.restore(root,backup)
    assert (root/'src/obsolete.py').read_bytes()==b'old'
    assert json.loads((root/'RELEASE-INFO.json').read_text())['version']=='0.9.0'


@pytest.mark.parametrize('path',['content/persona.md','config/local.json','data/admin.token','accounts/other/src/a.py',
                                 '../README.md','src/../../escape','src/CON.py','src/trailing.','src/a:stream'])
def test_archive_never_writes_private_or_unsafe_paths(tmp_path,path):
    z,release,_=package(tmp_path,extra={path:b'disallowed'})
    with pytest.raises(ValueError,match='update_archive_invalid'):archive.validate(z,release)


def test_checksum_identity_manifest_and_local_changes(tmp_path):
    z,release,files=package(tmp_path)
    with pytest.raises(ValueError,match='checksum'):archive.validate(z,release|{'sha256':'0'*64})
    with pytest.raises(ValueError,match='identity'):archive.validate(z,release|{'commit':'b'*40})
    root=tmp_path/'installed';installed(root,files);(root/'README.md').write_text('my modification')
    with pytest.raises(ValueError,match='local_code_modified'):archive.install_files(root,z,release,tmp_path/'backup')
    (root/'.git').mkdir()
    with pytest.raises(ValueError,match='source_checkout'):archive.install_files(root,z,release,tmp_path/'backup')


def test_mid_install_failure_restores_all_public_files(tmp_path,monkeypatch):
    _,_,old=package(tmp_path,release_version='0.9.0');root=tmp_path/'installed';installed(root,old)
    z,release,_=package(tmp_path);original=archive.os.replace;calls=0
    def fail(source,destination):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('synthetic write failure')
        original(source,destination)
    monkeypatch.setattr(archive.os,'replace',fail)
    with pytest.raises(OSError):archive.install_files(root,z,release,tmp_path/'backup')
    for name,body in old.items():assert (root/name).read_bytes()==body


@pytest.mark.asyncio
async def test_feed_selection_download_source_change_and_release_only(tmp_path):
    z,release,files=package(tmp_path);root=tmp_path/'installed';installed(root,files)
    service=UpdateService(root);url='https://downloads.example.test/updates.json'
    service.save(DEFAULTS|{'feed_url':url})
    calls=[]
    async def fetch(address,limit):
        calls.append(address)
        return json.dumps({'schema':1,'releases':[release]}).encode() if address==url else z.read_bytes()
    service.fetch=fetch
    result=await service.prepare();assert result['pending']['release']==release
    assert calls==[url,release['url']]
    service.save(DEFAULTS|{'feed_url':'https://another.example.test/updates.json'})
    assert service.status()['pending'] is None
    (root/'.git').mkdir()
    with pytest.raises(ValueError,match='release_installation'):await service.prepare()


@pytest.mark.asyncio
async def test_feed_rejects_cross_origin_and_older_versions(tmp_path):
    _,release,_=package(tmp_path);service=UpdateService(tmp_path);url='https://downloads.example.test/updates.json'
    service.save(DEFAULTS|{'feed_url':url})
    async def fetch(*args):return json.dumps({'schema':1,'releases':[release|{'url':'https://untrusted.test/program.zip'}]}).encode()
    service.fetch=fetch
    with pytest.raises(ValueError,match='origin_mismatch'):await service.check()
    async def older(*args):return json.dumps({'schema':1,'releases':[release|{'version':'0.8.0'}]}).encode()
    service.fetch=older;assert (await service.check())['check']['release'] is None
    assert version('0.9.0rc4')<version('0.9.0')<version('0.9.1')


def test_update_api_auth_origin_private_config_and_developer_mode(http):
    client,root,_,admin,bridge=http
    assert client.get('/admin/updates').status_code==401
    assert client.get('/admin/updates',headers=bridge).status_code==401
    assert not client.get('/admin/updates',headers=admin).json()['supported']
    body=DEFAULTS|{'feed_url':'https://downloads.example.test/updates.json'}
    assert client.put('/admin/updates',headers=admin|{'Origin':'https://foreign.test'},json=body).status_code==403
    assert client.put('/admin/updates',headers=admin,json=body).status_code==200
    assert client.post('/admin/updates/install',headers=admin,json={}).json()['detail']=='update_release_installation_required'


def test_detached_finished_process_does_not_block_update():
    import psutil
    from types import SimpleNamespace
    from azusa_bot.updates.worker import wait_stopped
    wait_stopped(SimpleNamespace(is_running=lambda:True,status=lambda:psutil.STATUS_ZOMBIE),timeout=.1)
    wait_stopped(SimpleNamespace(is_running=lambda:False),timeout=.1)


@pytest.mark.parametrize('fails',[False,True])
def test_worker_applies_or_rolls_back_without_touching_qq_or_private_data(tmp_path,monkeypatch,fails):
    import sys
    from azusa_bot.updates import worker
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    _,_,old=package(tmp_path,release_version='0.9.0')
    root=tmp_path/'worker 中文 space';installed(root,old)
    # This fixture's pseudo DB contains a sentinel, not SQLite; backup API tested separately.
    (root/'data/history.sqlite3').unlink()
    z,release,_=package(tmp_path,extra={'scripts/install_release.py':b'raise SystemExit(1)\n'} if fails else {})
    service=UpdateService(root);service.save(DEFAULTS|{'feed_url':'https://downloads.example.test/updates.json'})
    (service.directory/'pending.zip').write_bytes(z.read_bytes())
    service.write('pending.json',{'release':release,'feed_url':service.status()['settings']['feed_url']})
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    if fails:
        with pytest.raises(ValueError,match='dependency_install'):worker.main(root)
        assert json.loads((root/'RELEASE-INFO.json').read_text())['version']=='0.9.0'
    else:
        worker.main(root)
        assert json.loads((root/'RELEASE-INFO.json').read_text())['version']=='0.9.1'
    assert service.status()['operation']['state']==('failed' if fails else 'installed')
    assert (root/'content/persona.md').read_text()=='PRIVATE SENTINEL'
