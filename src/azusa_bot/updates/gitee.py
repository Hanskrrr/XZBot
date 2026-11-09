"""Public Gitee release discovery; downloads never carry publisher credentials."""
import json
import re
from urllib.parse import urljoin, urlsplit

DEFAULT_SOURCE = 'https://gitee.com/api/v5/repos/hanskrrr/XZBot/releases'


def repository(url):
    match = re.fullmatch(r'https://gitee\.com/api/v5/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases', url)
    return match.group(1) if match else None


def asset_url(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and p.netloc == 'gitee.com' and not p.query
            and not p.fragment and not p.path.startswith('/api/'))


def redirect_allowed(url):
    p = urlsplit(url)
    host = p.hostname or ''
    return (p.scheme == 'https' and not p.username and not p.password
            and not p.fragment and p.port in (None, 443)
            and (host == 'gitee.com' or host == 'giteeusercontent.com'
                 or host.endswith('.giteeusercontent.com')))


async def resolve(url, fetch, *, include_prerelease, version):
    repo = repository(url)
    rows = json.loads(await fetch(url, 2 * 1024 * 1024))
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('update_feed_invalid')
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or (row.get('prerelease') and not include_prerelease):
            continue
        match = re.fullmatch(r'v(\d+\.\d+\.\d+)(?:-rc\.(\d+))?', str(row.get('tag_name', '')))
        if not match or type(row.get('id')) is not int:
            continue
        v = match[1] + ('rc' + match[2] if match[2] else '')
        if 'rc' in v and not include_prerelease:
            continue
        candidates.append((version(v), v, row['id']))
    for _, v, release_id in sorted(candidates, reverse=True):
        attachments = json.loads(await fetch(f'{url}/{release_id}/attach_files', 2 * 1024 * 1024))
        if not isinstance(attachments, list) or len(attachments) > 100:
            raise ValueError('update_feed_invalid')
        assets = {}
        for item in attachments:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                raise ValueError('update_feed_invalid')
            name = item['name']
            if name in assets:
                raise ValueError('update_feed_invalid')
            target = urljoin('https://gitee.com', str(item.get('browser_download_url', '')))
            if not asset_url(target) or not urlsplit(target).path.startswith('/' + repo + '/'):
                raise ValueError('update_asset_origin_mismatch')
            assets[name] = target
        # Publisher uploads this commit marker only after all three ZIPs verify.
        if 'updates.json' not in assets:
            continue
        feed_url = assets['updates.json']
        feed = json.loads(await fetch(feed_url, 1024 * 1024))
        if not isinstance(feed, dict) or feed.get('schema') != 1 or not isinstance(feed.get('releases'), list):
            raise ValueError('update_feed_invalid')
        for item in feed['releases']:
            if (not isinstance(item, dict) or item.get('version') != v
                    or not isinstance(item.get('url'), str) or '/' in item['url']
                    or item['url'] not in assets or not item['url'].endswith('.zip')):
                raise ValueError('update_asset_origin_mismatch')
            item['url'] = assets[item['url']]
        return feed, feed_url
    return {'schema': 1, 'releases': []}, url
