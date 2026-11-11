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
    ('https://foruda.gitee.com/attach_file/123/file.zip?token=public', True),
    ('https://unknown.gitee.com/file.zip', False),
    ('https://foruda.gitee.com.evil.test/file.zip', False),
    ('https://attacker.giteeusercontent.com.evil.test/file', False),
    ('https://github.com/example/file.zip', False),
    ('http://gitee.com/file', False),
    ('http://foruda.gitee.com/file.zip', False),
    ('https://gitee.com@evil.test/file', False),
    ('https://user:password@foruda.gitee.com/file.zip', False),
    ('https://foruda.gitee.com:8443/file.zip', False),
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


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt_package', [False, True])
async def test_gitee_prepare_follows_real_cdn_chain_and_verifies_checksum(tmp_path, monkeypatch, corrupt_package):
    import azusa_bot.updates.service as mod
    archive, release, files = package(tmp_path)
    root = tmp_path / 'installed'
    installed(root, files)
    service = UpdateService(root)
    name = 'XZBot-0.9.1.zip'
    release_base = 'https://gitee.com/hanskrrr/XZBot/releases/download/v0.9.1/'
    attachment_base = 'https://gitee.com/hanskrrr/XZBot/attach_files/'
    cdn_base = 'https://foruda.gitee.com/attach_file/'
    feed_url, package_url = release_base + 'updates.json', release_base + name
    feed_attachment = attachment_base + '1/download/updates.json'
    package_attachment = attachment_base + '2/download/' + name
    feed_cdn = cdn_base + '123/updates.json?token=public-feed'
    package_cdn = cdn_base + '456/' + name + '?token=public-package'
    attachments_url = gitee.DEFAULT_SOURCE + '/10/attach_files'
    responses = {
        gitee.DEFAULT_SOURCE: httpx.Response(200, json=[{'id': 10, 'tag_name': 'v0.9.1'}]),
        attachments_url: httpx.Response(200, json=[
            {'name': 'updates.json', 'browser_download_url': feed_url},
            {'name': name, 'browser_download_url': package_url},
        ]),
        feed_url: httpx.Response(302, headers={'location': feed_attachment}),
        feed_attachment: httpx.Response(302, headers={'location': feed_cdn}),
        feed_cdn: httpx.Response(200, json={'schema': 1, 'releases': [release | {'url': name}]}),
        package_url: httpx.Response(302, headers={'location': package_attachment}),
        package_attachment: httpx.Response(302, headers={'location': package_cdn}),
        package_cdn: httpx.Response(200, content=b'corrupt package' if corrupt_package else archive.read_bytes()),
    }
    calls = []
    real_client = httpx.AsyncClient
    def handler(request):
        url = str(request.url)
        calls.append(url)
        assert 'authorization' not in request.headers
        assert 'cookie' not in request.headers
        assert url in responses
        return responses[url]
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mod.socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('1.1.1.1', 443))])
    if corrupt_package:
        with pytest.raises(ValueError, match='update_checksum_mismatch'):
            await service.prepare()
        assert service.status()['pending'] is None
        assert not (service.directory / 'pending.zip').exists()
    else:
        result = await service.prepare()
        assert result['pending']['release']['url'] == package_url
        assert (service.directory / 'pending.zip').read_bytes() == archive.read_bytes()
    assert calls == [gitee.DEFAULT_SOURCE, attachments_url, feed_url, feed_attachment, feed_cdn,
                     package_url, package_attachment, package_cdn]


@pytest.mark.asyncio
async def test_gitee_cdn_cannot_redirect_to_an_untrusted_host(monkeypatch):
    import azusa_bot.updates.service as mod
    url = 'https://gitee.com/example/project/attach_files/1/download/file.zip'
    cdn = 'https://foruda.gitee.com/attach_file/123/file.zip?token=public'
    calls = []
    real_client = httpx.AsyncClient
    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) in (url, cdn)
        target = cdn if str(request.url) == url else 'https://evil.test/package.zip'
        return httpx.Response(302, headers={'location': target})
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mod.socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('1.1.1.1', 443))])
    with pytest.raises(ValueError, match='update_asset_origin_mismatch'):
        await mod.download(url, 100)
    assert calls == [url, cdn]


@pytest.mark.asyncio
@pytest.mark.parametrize('address', ['127.0.0.1', '192.168.0.1', '169.254.169.254'])
async def test_gitee_cdn_dns_cannot_resolve_to_a_private_address(monkeypatch, address):
    import azusa_bot.updates.service as mod
    url = 'https://gitee.com/example/project/attach_files/1/download/file.zip'
    cdn = 'https://foruda.gitee.com/attach_file/123/file.zip?token=public'
    calls = []
    real_client = httpx.AsyncClient
    def handler(request):
        calls.append(str(request.url))
        assert str(request.url) == url
        return httpx.Response(302, headers={'location': cdn})
    def addresses(host, *args, **kwargs):
        return [(2, 1, 6, '', (address if host == 'foruda.gitee.com' else '1.1.1.1', 443))]
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(mod.socket, 'getaddrinfo', addresses)
    with pytest.raises(ValueError, match='update_public_host_required'):
        await mod.download(url, 100)
    assert calls == [url]
