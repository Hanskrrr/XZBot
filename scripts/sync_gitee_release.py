"""Mirror verified GitHub packages to Gitee. Credentials come only from env.

Uses Gitee v5 Releases/attach_files APIs. Never deletes releases or attachments.
GitHub's gh CLI is already available on the Actions runner. --dry-run downloads
and validates the source release without reading a Gitee credential or writing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import httpx

PLATFORMS = {'windows': 'x86_64', 'macos': 'aarch64', 'linux': 'x86_64'}
CHECKS = ('archive_install_test', 'core_bridge_test', 'unicode_directory_test')
MAX_FILE = 100 * 1024 * 1024
API = 'https://gitee.com/api/v5'


class UploadReader:
    """Report streamed bytes without logging request headers or response bodies."""

    def __init__(self, source, name):
        self.source = source
        self.name = name
        self.started = time.monotonic()
        self.sent = 0
        self.reported = 0

    def __getattr__(self, name):
        return getattr(self.source, name)

    def read(self, size=-1):
        require(time.monotonic() - self.started <= 600, 'gitee_upload_body_deadline')
        block = self.source.read(size)
        self.sent += len(block)
        if self.sent - self.reported >= 5 * 1024 * 1024 or not block:
            print(f'Upload stream: {self.name} {self.sent / 1024 / 1024:.1f} MiB', flush=True)
            self.reported = self.sent
        return block


class SyncError(Exception):
    """Only locally defined, credential-free codes may be shown in CI logs."""


def require(condition, code):
    if not condition:
        raise SyncError(code)


def repository(value):
    require(bool(re.fullmatch(r'[\w.-]+/[\w.-]+', value, flags=re.ASCII)), 'invalid_repository')
    return value


def version_for(tag):
    require(bool(re.fullmatch(r'v\d+\.\d+\.\d+(?:-rc\.\d+)?', tag)), 'invalid_release_tag')
    return tag[1:].replace('-rc.', 'rc')


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def gh(*args):
    try:
        return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise SyncError('github_cli_failed') from None


def download_source(repo, tag, directory):
    """Use explicit artifact names; never run downloaded code or release notes."""
    repository(repo)
    if not tag:
        releases = json.loads(gh('api', f'repos/{repo}/releases?per_page=100'))
        tag = next((r['tag_name'] for r in releases if not r['draft']), '')
    version = version_for(tag)
    meta = json.loads(gh('release', 'view', tag, '--repo', repo, '--json',
                         'tagName,name,body,isDraft,isPrerelease,assets'))
    require(meta['tagName'] == tag and not meta['isDraft'], 'github_release_not_published')
    commit = json.loads(gh('api', f'repos/{repo}/commits/{tag}'))['sha']
    require(bool(re.fullmatch(r'[0-9a-f]{40}', commit)), 'invalid_source_commit')
    names = expected_names(version)
    assets = {a['name']: a for a in meta['assets']}
    require(all(name in assets for name in names), 'github_release_missing_assets')
    require(all(0 < assets[name]['size'] <= MAX_FILE for name in names), 'asset_size_limit')
    patterns = [value for name in names for value in ('--pattern', name)]
    print(f'Downloading verified GitHub release: {tag}', flush=True)
    gh('release', 'download', tag, '--repo', repo, '--dir', str(directory), *patterns)
    validate_packages(directory, meta, commit)
    return meta, commit


def expected_names(version):
    names = []
    for platform, arch in PLATFORMS.items():
        base = f'XZBot-{version}-{platform}-{arch}'
        names.extend([base + '.zip', base + '.zip.sha256', base + '.verification.json'])
    return names


def validate_packages(directory, meta, commit):
    require(not meta['isDraft'], 'github_release_not_published')
    version = version_for(meta['tagName'])
    for platform, arch in PLATFORMS.items():
        base = f'XZBot-{version}-{platform}-{arch}'
        archive = directory / (base + '.zip')
        for name in (base + '.zip', base + '.zip.sha256', base + '.verification.json'):
            path = directory / name
            require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= MAX_FILE,
                    'invalid_local_asset')
        report = json.loads((directory / (base + '.verification.json')).read_text())
        checksum = (directory / (base + '.zip.sha256')).read_text().strip().split()
        require(len(checksum) == 2 and checksum[1].lstrip('*') == archive.name, 'invalid_checksum_file')
        require(digest(archive) == checksum[0] == report['zip_sha256'], 'source_digest_mismatch')
        require(all(report.get(k) == v for k, v in
                    {'version': version, 'platform': platform, 'arch': arch, 'commit': commit}.items()),
                'source_report_mismatch')
        require(all(report.get(k) == 'passed' for k in CHECKS), 'source_verification_not_passed')


class Gitee:
    def __init__(self, repo, token, *, client=None, sleep=time.sleep):
        self.base = f'{API}/repos/{repository(repo)}'
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(120, connect=20), follow_redirects=False)
        self.sleep = sleep

    def request(self, method, path, *, missing=False, **kwargs):
        # Token in a header only: never in a URL, redirect, log or CLI argument.
        for attempt in range(3 if method == 'GET' else 1):
            try:
                response = self.client.request(method, self.base + path,
                                               headers={'Authorization': f'Bearer {self.token}'}, **kwargs)
            except httpx.HTTPError as error:
                if method == 'GET' and attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                phase = {httpx.ConnectTimeout: 'connect_timeout', httpx.WriteTimeout: 'write_timeout',
                         httpx.ReadTimeout: 'read_timeout', httpx.PoolTimeout: 'pool_timeout'}.get(type(error), 'network_error')
                raise SyncError('gitee_' + phase + ('_write_outcome_uncertain' if method != 'GET' else '')) from None
            if response.status_code == 404 and missing:
                return None
            if response.status_code in (429, 500, 502, 503, 504) and method == 'GET' and attempt < 2:
                self.sleep(2 ** attempt)
                continue
            require(200 <= response.status_code < 300, f'gitee_http_{response.status_code}')
            try:
                return response.json()
            except ValueError:
                raise SyncError('gitee_invalid_response') from None
        raise SyncError('gitee_read_failed')

    def wait_for_tag(self, tag, commit, attempts=13):
        for attempt in range(attempts):
            current = self.request('GET', f'/commits/{tag}', missing=True)
            if current and current.get('sha') == commit:
                return
            if attempt + 1 < attempts:
                self.sleep(30)
        raise SyncError('gitee_tag_not_synced_check_repository_mirror')

    def attachments(self, release_id):
        result = []
        for page in range(1, 11):
            batch = self.request('GET', f'/releases/{release_id}/attach_files',
                                 params={'per_page': 100, 'page': page})
            require(isinstance(batch, list), 'gitee_invalid_attachments')
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise SyncError('gitee_too_many_attachments')

    def remote_digest(self, asset):
        """Public downloads use NO credential, even on the initial Gitee URL."""
        url = asset['browser_download_url']
        if url.startswith('/'):
            url = urljoin('https://gitee.com', url)
        for _ in range(6):
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            allowed = any(host == domain or host.endswith('.' + domain)
                          for domain in ('gitee.com', 'giteeusercontent.com'))
            require(parsed.scheme == 'https' and allowed and not parsed.username and not parsed.password
                    and parsed.port in (None, 443), 'gitee_unsafe_attachment_url')
            try:
                with self.client.stream('GET', url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers['location'])
                        continue
                    require(response.status_code == 200, f'gitee_download_http_{response.status_code}')
                    total = 0
                    checksum = hashlib.sha256()
                    for block in response.iter_bytes():
                        total += len(block)
                        require(total <= MAX_FILE, 'gitee_download_too_large')
                        checksum.update(block)
                    return checksum.hexdigest()
            except httpx.HTTPError:
                raise SyncError('gitee_download_failed') from None
        raise SyncError('gitee_download_redirect_limit')

    def find_verified(self, release_id, path):
        matches = [a for a in self.attachments(release_id) if a['name'] == path.name]
        require(len(matches) <= 1, 'gitee_duplicate_attachment_names')
        if not matches:
            return False
        require(self.remote_digest(matches[0]) == digest(path), 'gitee_existing_asset_differs_no_overwrite')
        return True

    def mirror(self, meta, commit, directory):
        # Validate again here so callers cannot accidentally publish unverified files.
        validate_packages(directory, meta, commit)
        tag = meta['tagName']
        print(f'Checking Gitee mirror tag: {tag}', flush=True)
        self.wait_for_tag(tag, commit)
        fields = {'tag_name': tag, 'name': meta['name'] or tag, 'body': meta['body'] or '',
                  'prerelease': str(bool(meta['isPrerelease'])).lower()}
        release = self.request('GET', f'/releases/tags/{tag}', missing=True)
        if not release:
            pending = dict(fields, target_commitish=commit, prerelease='true',
                           body='附件正在同步；完成前请使用 GitHub 上的已验证发行版。')
            try:
                release = self.request('POST', '/releases', data=pending)
            except SyncError:
                # POST may have committed despite a timeout. Read before considering another write.
                release = self.request('GET', f'/releases/tags/{tag}', missing=True)
                require(release is not None, 'gitee_release_creation_uncertain_rerun')
        release_id = release['id']
        require(isinstance(release_id, int), 'gitee_invalid_release_id')
        uploaded = skipped = 0
        for name in expected_names(version_for(tag)):
            path = directory / name
            print(f'Checking attachment: {name}', flush=True)
            if self.find_verified(release_id, path):
                skipped += 1
                continue
            try:
                print(f'Uploading: {name} ({path.stat().st_size / 1024 / 1024:.1f} MiB)', flush=True)
                with path.open('rb') as source:
                    self.request('POST', f'/releases/{release_id}/attach_files',
                                 files={'file': (name, UploadReader(source, name), 'application/octet-stream')})
            except SyncError as error:
                # Never blindly retry an upload: duplicate files can be created on Gitee.
                if not self.find_verified(release_id, path):
                    raise error
            require(self.find_verified(release_id, path), 'gitee_uploaded_asset_not_visible_rerun')
            uploaded += 1
            print(f'Verified: {name}', flush=True)
        # Only promote the release and its actual notes after all 9 files are verified.
        self.request('PATCH', f'/releases/{release_id}', data=fields)
        final = self.request('GET', f'/releases/tags/{tag}')
        require(all(final.get(k) == v for k, v in fields.items() if k != 'prerelease')
                and final.get('prerelease') == bool(meta['isPrerelease']), 'gitee_release_notes_mismatch')
        return uploaded, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', default='')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        source = repository(os.environ.get('GITHUB_REPOSITORY', ''))
        target = repository(os.environ.get('GITEE_REPOSITORY') or source.split('/')[0].lower() + '/' + source.split('/')[1])
        if not args.dry_run:
            require(bool(os.environ.get('GITEE_TOKEN')), 'missing_GITEE_TOKEN')
        with tempfile.TemporaryDirectory(prefix='xzbot-release-') as tmp:
            directory = Path(tmp)
            meta, commit = download_source(source, args.tag, directory)
            if args.dry_run:
                message = f"仅验证：{meta['tagName']} 的三个平台、9 个附件通过校验；未写入 Gitee。"
            else:
                with httpx.Client(timeout=httpx.Timeout(120, connect=20), follow_redirects=False) as client:
                    uploaded, skipped = Gitee(target, os.environ['GITEE_TOKEN'], client=client).mirror(meta, commit, directory)
                message = f"{meta['tagName']} 同步完成：上传 {uploaded}，已有且校验一致 {skipped}；发行说明一致。"
                message += f'\n\n[Gitee 发行版](https://gitee.com/{target}/releases/tag/{meta["tagName"]})'
        print(message)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
                output.write(message + '\n')
        return 0
    except SyncError as error:
        print(f'Sync failed: {error}', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        # Do not expose raw API bodies, command output, URLs or secrets in tracebacks.
        print('Sync failed: invalid_release_data_or_local_io', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
