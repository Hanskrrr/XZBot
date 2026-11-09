"""Public GitHub Releases discovery. No account token is read or transmitted."""
import json
import re
from urllib.parse import urlsplit

def repository(url):
    match = re.fullmatch(r'https://api\.github\.com/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases',url)
    return match.group(1) if match else None


def asset_url(url):
    p=urlsplit(url)
    return (p.scheme=='https' and p.netloc=='github.com' and not p.query and not p.fragment
            and re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^/]+/[^/]+',p.path) is not None)


def redirect_allowed(url):
    p=urlsplit(url)
    return (p.scheme=='https' and not p.username and not p.password and not p.fragment
            and p.netloc in {'release-assets.githubusercontent.com','objects.githubusercontent.com'})


async def resolve(url, fetch, *, include_prerelease, version):
    repo=repository(url)
    rows=json.loads(await fetch(url,2*1024*1024))
    if not isinstance(rows,list) or len(rows)>100:raise ValueError('update_feed_invalid')
    candidates=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('draft') or (row.get('prerelease') and not include_prerelease):continue
        match=re.fullmatch(r'v(\d+\.\d+\.\d+)(?:-rc\.(\d+))?',str(row.get('tag_name','')))
        if not match:continue
        v=match[1]+('rc'+match[2] if match[2] else '')
        if 'rc' in v and not include_prerelease:continue
        assets=row.get('assets',[])
        if not isinstance(assets,list):raise ValueError('update_feed_invalid')
        names={a.get('name') for a in assets if isinstance(a,dict)}
        if 'updates.json' in names:candidates.append((version(v),v,row['tag_name'],names))
    if not candidates:return {'schema':1,'releases':[]},url
    _,v,tag,names=max(candidates,key=lambda c:c[0])
    base=f'https://github.com/{repo}/releases/download/{tag}/'
    feed=json.loads(await fetch(base+'updates.json',1024*1024))
    if not isinstance(feed,dict) or feed.get('schema')!=1 or not isinstance(feed.get('releases'),list):
        raise ValueError('update_feed_invalid')
    for r in feed['releases']:
        if (not isinstance(r,dict) or r.get('version')!=v or not isinstance(r.get('url'),str)
                or '/' in r['url'] or r['url'] not in names or not r['url'].endswith('.zip')):
            raise ValueError('update_asset_origin_mismatch')
    return feed,base+'updates.json'
