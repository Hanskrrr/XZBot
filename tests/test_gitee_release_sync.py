"""Exercise release mirroring, interrupted uploads and credential boundaries offline."""
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

spec = importlib.util.spec_from_file_location('gitee_sync', Path(__file__).parents[1] / 'scripts/sync_gitee_release.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
COMMIT = 'a' * 40
TOKEN = 'unit-test-only-publishing-credential'


@pytest.fixture
def bundle(tmp_path):
    meta = {'tagName': 'v0.9.0-rc.6', 'name': 'XZBot 候选版', 'body': '## 新功能\n三端安装包与说明。\n',
            'isDraft': False, 'isPrerelease': True}
    for platform, arch in sync.PLATFORMS.items():
        base = f'XZBot-0.9.0rc6-{platform}-{arch}'
        data = ('verified test artifact: ' + platform).encode()
        checksum = hashlib.sha256(data).hexdigest()
        (tmp_path / (base + '.zip')).write_bytes(data)
        (tmp_path / (base + '.zip.sha256')).write_text(checksum + '  ' + base + '.zip\n')
        report = {'version': '0.9.0rc6', 'platform': platform, 'arch': arch, 'commit': COMMIT,
                  'zip_sha256': checksum, **dict.fromkeys(sync.CHECKS, 'passed')}
        (tmp_path / (base + '.verification.json')).write_text(json.dumps(report))
    return meta, tmp_path


class Server:
    def __init__(self, directory):
        self.directory = directory
        self.release = None
        self.files = {}
        self.requests = []
        self.posts = []
        self.sha = COMMIT
        self.lost_upload_ack = False
        self.lost_create_ack = False
        self.fail_after = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.startswith('/downloads/'):
            assert 'Authorization' not in request.headers
            return httpx.Response(200, content=self.files[path.rsplit('/', 1)[-1]])
        assert request.headers['Authorization'] == 'Bearer ' + TOKEN
        assert TOKEN not in str(request.url)
        if '/commits/' in path:
            return httpx.Response(200, json={'sha': self.sha}) if self.sha else httpx.Response(404)
        if '/releases/tags/' in path:
            return httpx.Response(200, json=self.release) if self.release else httpx.Response(404)
        if path.endswith('/attach_files'):
            if request.method == 'GET':
                return httpx.Response(200, json=[{'id': i, 'name': n, 'browser_download_url': f'https://gitee.com/downloads/{n}'}
                                                 for i, n in enumerate(self.files)])
            body = request.read()
            if self.fail_after is not None and len(self.files) >= self.fail_after:
                return httpx.Response(503, text='sensitive server response ' + TOKEN)
            name = next(p.name for p in self.directory.iterdir() if f'filename="{p.name}"'.encode() in body)
            assert (self.directory / name).read_bytes() in body
            self.posts.append(name)
            self.files[name] = (self.directory / name).read_bytes()
            if self.lost_upload_ack:
                self.lost_upload_ack = False
                raise httpx.ReadTimeout('sensitive error ' + TOKEN, request=request)
            return httpx.Response(201, json={'name': name})
        fields = {k: v[0] for k, v in parse_qs(request.read().decode(), keep_blank_values=True).items()}
        if request.method in ('POST', 'PATCH'):
            self.release = {'id': 7, **fields, 'prerelease': fields['prerelease'] == 'true'}
            if request.method == 'POST' and self.lost_create_ack:
                self.lost_create_ack = False
                raise httpx.ReadTimeout(TOKEN, request=request)
            return httpx.Response(201 if request.method == 'POST' else 200, json=self.release)
        raise AssertionError((request.method, path))

    def api(self):
        client = httpx.Client(transport=httpx.MockTransport(self))
        api = sync.Gitee('example/XZBot', TOKEN, client=client, sleep=lambda _: None)
        def upload(url, token, path):
            with path.open('rb') as source:
                api.request('POST', url.removeprefix(api.base), files={'file': (path.name, source, 'application/octet-stream')})
        api.uploader = upload
        return api


def test_complete_publish_and_idempotent_rerun(bundle):
    meta, directory = bundle
    server = Server(directory)
    assert server.api().mirror(meta, COMMIT, directory) == (9, 0)
    assert server.release['body'] == meta['body']
    assert server.release['name'] == meta['name']
    assert server.release['prerelease'] is True
    assert server.api().mirror(meta, COMMIT, directory) == (0, 9)
    assert len(server.posts) == 9
    assert not any(r.method == 'DELETE' for r in server.requests)


@pytest.mark.parametrize('fault', ['lost_upload_ack', 'lost_create_ack'])
def test_lost_ack_is_reconciled_without_duplicate_writes(bundle, fault):
    meta, directory = bundle
    server = Server(directory)
    setattr(server, fault, True)
    assert server.api().mirror(meta, COMMIT, directory) == (9, 0)
    assert len(server.posts) == len(set(server.posts)) == 9
    assert sum(r.method == 'POST' and r.url.path.endswith('/releases') for r in server.requests) == 1


def test_partial_upload_can_resume_and_notes_only_finalized_on_success(bundle):
    meta, directory = bundle
    server = Server(directory)
    server.fail_after = 4
    with pytest.raises(sync.SyncError, match='gitee_http_503') as error:
        server.api().mirror(meta, COMMIT, directory)
    assert TOKEN not in str(error.value)
    assert server.release['body'] != meta['body']
    server.fail_after = None
    assert server.api().mirror(meta, COMMIT, directory) == (5, 4)
    assert len(server.posts) == 9
    assert server.release['body'] == meta['body']


def test_same_name_different_bytes_is_never_overwritten(bundle):
    meta, directory = bundle
    server = Server(directory)
    server.api().mirror(meta, COMMIT, directory)
    server.files[next(iter(server.files))] = b'changed attachment'
    before = len(server.posts)
    with pytest.raises(sync.SyncError, match='existing_asset_differs_no_overwrite'):
        server.api().mirror(meta, COMMIT, directory)
    assert len(server.posts) == before


@pytest.mark.parametrize('damage', ['zip', 'checksum', 'report_commit', 'report_test', 'missing', 'draft'])
def test_source_validation_before_any_gitee_request(bundle, damage):
    meta, directory = bundle
    archive = next(directory.glob('*.zip'))
    if damage == 'zip':
        archive.write_bytes(b'tampered')
    elif damage == 'checksum':
        archive.with_suffix('.zip.sha256').write_text('0' * 64 + '  ' + archive.name)
    elif damage.startswith('report_'):
        path = archive.with_suffix('.verification.json')
        report = json.loads(path.read_text())
        report['commit' if damage == 'report_commit' else 'archive_install_test'] = 'wrong'
        path.write_text(json.dumps(report))
    elif damage == 'missing':
        archive.unlink()
    else:
        meta['isDraft'] = True
    server = Server(directory)
    with pytest.raises(sync.SyncError):
        server.api().mirror(meta, COMMIT, directory)
    assert not server.requests


@pytest.mark.parametrize('sha', [None, 'b' * 40])
def test_missing_or_stale_mirror_tag_never_creates_release(bundle, sha):
    meta, directory = bundle
    server = Server(directory)
    server.sha = sha
    with pytest.raises(sync.SyncError, match='gitee_tag_not_synced'):
        server.api().mirror(meta, COMMIT, directory)
    assert not any(r.method != 'GET' for r in server.requests)


def test_stable_release_is_promoted_only_after_verification(bundle):
    meta, directory = bundle
    meta['isPrerelease'] = False
    server = Server(directory)
    server.api().mirror(meta, COMMIT, directory)
    create = next(r for r in server.requests if r.method == 'POST' and r.url.path.endswith('/releases'))
    assert parse_qs(create.content.decode())['prerelease'] == ['true']
    assert server.release['prerelease'] is False


def test_download_redirect_never_receives_token_or_follows_untrusted_host():
    requests = []
    def handler(request):
        requests.append(request)
        assert 'Authorization' not in request.headers
        return httpx.Response(302, headers={'location': 'https://attacker.example/stolen'})
    api = sync.Gitee('example/XZBot', TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(sync.SyncError, match='unsafe_attachment_url'):
        api.remote_digest({'browser_download_url': 'https://gitee.com/file'})
    assert len(requests) == 1


def test_api_does_not_follow_redirect_or_print_sensitive_response():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'https://attacker.example/'}, text=TOKEN)
    api = sync.Gitee('example/XZBot', TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(sync.SyncError, match='gitee_http_302') as error:
        api.request('GET', '/releases')
    assert TOKEN not in str(error.value)
    assert len(requests) == 1


def test_source_selection_includes_rc_and_only_nine_expected_assets(bundle, monkeypatch):
    meta, directory = bundle
    meta['assets'] = [{'name': p.name, 'size': p.stat().st_size} for p in directory.iterdir()]
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[0] == 'api':
            return json.dumps([{'draft': False, 'tag_name': meta['tagName']}]) if '/releases?' in args[1] else json.dumps({'sha': COMMIT})
        return json.dumps(meta) if args[1] == 'view' else ''
    monkeypatch.setattr(sync, 'gh', fake_gh)
    actual, commit = sync.download_source('example/XZBot', '', directory)
    assert actual['tagName'] == 'v0.9.0-rc.6' and commit == COMMIT
    download = next(c for c in calls if c[:2] == ('release', 'download'))
    assert download.count('--pattern') == 9
    assert not any('updates.json' in a for a in download)


def test_dry_run_needs_no_gitee_token_and_performs_no_write(bundle, monkeypatch, capsys):
    meta, directory = bundle
    monkeypatch.setenv('GITHUB_REPOSITORY', 'example/XZBot')
    monkeypatch.delenv('GITEE_TOKEN', raising=False)
    monkeypatch.setattr(sync, 'download_source', lambda *a: (meta, COMMIT))
    monkeypatch.setattr(sync.Gitee, 'mirror', lambda *a: pytest.fail('dry-run must not publish'))
    monkeypatch.setattr('sys.argv', ['sync', '--dry-run'])
    assert sync.main() == 0
    assert '未写入 Gitee' in capsys.readouterr().out


@pytest.mark.parametrize('status,exit_code,expected', [('201',0,None), ('000',28,'gitee_upload_curl_28'), ('413',0,'gitee_http_413')])
def test_curl_upload_keeps_token_off_argv_and_enforces_total_timeout(tmp_path, monkeypatch, capsys, status, exit_code, expected):
    from types import SimpleNamespace
    path = tmp_path / 'package.zip'
    path.write_bytes(b'package')
    def run(command, **kwargs):
        assert TOKEN not in repr(command)
        assert command[command.index('--header') + 1] == '@-'
        assert kwargs['input'] == f'Authorization: Bearer {TOKEN}\n'
        assert command[command.index('--max-time') + 1] == '300'
        assert '--location' not in command
        assert kwargs['timeout'] == 330
        return SimpleNamespace(stdout=f'{status} 1234 512', stderr=TOKEN, returncode=exit_code)
    monkeypatch.setattr(sync.subprocess, 'run', run)
    if expected:
        with pytest.raises(sync.SyncError, match=expected):
            sync.curl_upload('https://gitee.com/api/v5/upload', TOKEN, path)
    else:
        sync.curl_upload('https://gitee.com/api/v5/upload', TOKEN, path)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_curl_upload_rejects_header_injection(tmp_path):
    with pytest.raises(sync.SyncError, match='invalid_credential_format'):
        sync.curl_upload('https://gitee.com/api/v5/upload', 'invalid\nheader: value', tmp_path / 'a.zip')
