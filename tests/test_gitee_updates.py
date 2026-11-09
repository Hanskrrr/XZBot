"""Gitee-only update resolution, incomplete publication and CDN trust boundaries."""
import json
import httpx
import pytest
from azusa_bot.updates import gitee
from azusa_bot.updates.service import UpdateService, DEFAULTS
from test_updates import package, installed


@pytest.mark.asyncio
async def test_gitee_update_downloads_verified_package_without_github(tmp_path):
    archive, release, files = package(tmp_path)
    root = tmp_path / 'installed'
    installed(root, files)
    service = UpdateService(root)
    base = 'https://gitee.com/hanskrrr/XZBot/attach_files/'
    calls = []
    rows = [{'id': 10, 'tag_name': 'v0.9.1', 'prerelease': False},
            {'id': 11, 'tag_name': 'v0.9.2', 'prerelease': False}]
    name = 'XZBot-0.9.1.zip'
    attachments = [{'name': 'updates.json', 'browser_download_url': base+'1/download/updates.json'},
                   {'name': name, 'browser_download_url': base+'2/download/'+name}]
    async def fetch(url, limit):
        calls.append(url)
        assert 'github' not in url
        if url == gitee.DEFAULT_SOURCE: result = rows
        elif url.endswith('/11/attach_files'): result = attachments[1:]  # Interrupted newer upload.
        elif url.endswith('/10/attach_files'): result = attachments
        elif url.endswith('/updates.json'): result = {'schema': 1, 'releases': [release | {'url': name}]}
        elif url.endswith('/'+name): return archive.read_bytes()
        else: pytest.fail(url)
        return json.dumps(result).encode()
    service.fetch = fetch
    result = await service.prepare()
    assert result['source_kind'] == 'official'
    assert result['pending']['release']['url'] == base+'2/download/'+name
    assert len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['foreign_asset', 'foreign_feed_entry', 'wrong_version', 'duplicate'])
async def test_gitee_rejects_confused_or_foreign_assets(tmp_path, problem):
    _, release, _ = package(tmp_path)
    prefix = 'https://gitee.com/hanskrrr/XZBot/attach_files/'
    rows = [{'id': 1, 'tag_name': 'v0.9.1'}]
    assets = [{'name': 'updates.json', 'browser_download_url': prefix+'1/download'},
              {'name': 'xz.zip', 'browser_download_url': prefix+'2/download'}]
    feed = {'schema': 1, 'releases': [release | {'url': 'xz.zip'}]}
    if problem == 'foreign_asset': assets[0]['browser_download_url'] = 'https://gitee.com/attacker/project/file'
    if problem == 'foreign_feed_entry': feed['releases'][0]['url'] = 'https://github.com/example/file.zip'
    if problem == 'wrong_version': feed['releases'][0]['version'] = '0.9.2'
    if problem == 'duplicate': assets.append(assets[0])
    async def fetch(url, limit):
        return json.dumps(rows if url == gitee.DEFAULT_SOURCE else assets if url.endswith('/attach_files') else feed).encode()
    service = UpdateService(tmp_path); service.fetch = fetch
    with pytest.raises(ValueError, match='update_(asset_origin_mismatch|feed_invalid)'):
        await service.check()


@pytest.mark.asyncio
async def test_gitee_prereleases_and_incomplete_release_are_not_eligible(tmp_path):
    service = UpdateService(tmp_path)
    service.save(DEFAULTS | {'include_prerelease': False})
    async def fetch(url, limit):
        assert url == gitee.DEFAULT_SOURCE
        return json.dumps([{'id': 1, 'tag_name': 'v9.0.0-rc.1', 'prerelease': True}]).encode()
    service.fetch = fetch
    assert (await service.check())['check']['release'] is None


def test_only_former_official_repository_migrates(tmp_path):
    service = UpdateService(tmp_path)
    former = 'https://api.github.com/repos/' + gitee.repository(gitee.DEFAULT_SOURCE) + '/releases'
    service.write('settings.json', DEFAULTS | {'feed_url': former})
    service.write('pending.json', {'feed_url': former, 'release': {'version': '0.9.1'}})
    service.write('check.json', {'feed_url': former})
    assert service.status()['settings']['feed_url'] == gitee.DEFAULT_SOURCE
    assert service.status()['pending'] is None
    assert service.status()['check'] == {}
    custom = 'https://api.github.com/repos/other/project/releases'
    service.save(DEFAULTS | {'feed_url': custom})
    assert service.status()['settings']['feed_url'] == custom


@pytest.mark.asyncio
@pytest.mark.parametrize('target,allowed', [
    ('https://download.giteeusercontent.com/signed?token=public', True),
    ('https://attacker.giteeusercontent.com.evil.test/file', False),
    ('https://github.com/example/file.zip', False),
    ('http://gitee.com/file', False),
    ('https://gitee.com@evil.test/file', False),
])
async def test_gitee_download_redirects_never_escape_provider(monkeypatch, target, allowed):
    import azusa_bot.updates.service as mod
    real_client = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert 'authorization' not in request.headers
        return httpx.Response(302, headers={'location': target}) if len(calls) == 1 else httpx.Response(200, content=b'zip')
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mod.socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('1.1.1.1', 443))])
    url = 'https://gitee.com/example/project/attach_files/1/download/file.zip'
    if allowed:
        assert await mod.download(url, 100) == b'zip'
    else:
        with pytest.raises(ValueError, match='origin_mismatch'):
            await mod.download(url, 100)
        assert len(calls) == 1
