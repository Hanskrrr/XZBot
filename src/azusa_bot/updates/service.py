"""Opt-in HTTPS update feed, bounded downloads and a staged native installer."""
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit
import httpx
from .. import __version__
from ..settings import private_write
from ..runtime_layout import runtime_layout
from ..host import protect
from .archive import validate

DEFAULTS={'feed_url':'','auto_check':True,'auto_download':False,'include_prerelease':True}


def version(value):
    match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?',value)
    if not match:raise ValueError('update_version_invalid')
    a,b,c,rc=match.groups()
    return int(a),int(b),int(c),int(rc) if rc else 1000000


def https_url(value):
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None,443) or parsed.query):
        raise ValueError('update_https_url_required')
    if parsed.hostname in {'localhost','localhost.localdomain'} or parsed.hostname.endswith(('.local','.localhost')):
        raise ValueError('update_public_host_required')
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:raise ValueError('update_public_host_required')
    except ValueError as error:
        if str(error)=='update_public_host_required':raise
    return value


async def download(url, limit):
    https_url(url)
    addresses=await asyncio.get_running_loop().run_in_executor(None,lambda:socket.getaddrinfo(urlsplit(url).hostname,443,type=socket.SOCK_STREAM))
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('update_public_host_required')
    # A redirect to another origin is never implicitly trusted as a code publisher.
    async with httpx.AsyncClient(timeout=60,follow_redirects=False,trust_env=False) as client:
        async with client.stream('GET',url,headers={'User-Agent':'XZBot-updater/1'}) as response:
            if response.status_code!=200:raise ValueError('update_download_failed')
            chunks=[];size=0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>limit:raise ValueError('update_download_too_large')
                chunks.append(chunk)
    return b''.join(chunks)


class UpdateService:
    def __init__(self,root):
        self.root=Path(root);self.layout=runtime_layout(self.root)
        self.directory=self.root/'data/updates';self.lock=asyncio.Lock()
        self.error='';self.fetch=download

    def read(self,name,default):
        path=self.directory/name
        if self.directory.is_symlink():raise ValueError('update_state_invalid')
        if not path.exists():return default
        if path.is_symlink() or path.stat().st_size>2*1024*1024:raise ValueError('update_state_invalid')
        return json.loads(path.read_text())

    def write(self,name,value):
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        protect(self.directory)
        private_write(self.directory/name,json.dumps(value,ensure_ascii=False))

    def supported(self):
        return (not self.layout.is_managed and not (self.root/'.git').exists()
                and (self.root/'RELEASE-INFO.json').is_file() and (self.root/'RELEASE-FILES.json').is_file())

    def status(self):
        return {'current_version':__version__,'supported':self.supported(),
                'reason':('manager_only' if self.layout.is_managed else 'source_checkout' if (self.root/'.git').exists() else 'release_manifest_required') if not self.supported() else '',
                'settings':DEFAULTS|self.read('settings.json',{}),'check':self.read('check.json',{}),
                'pending':self.read('pending.json',None),'operation':self.read('operation.json',{}),'error':self.error}

    def save(self,body):
        if not isinstance(body,dict) or set(body)!=set(DEFAULTS) or any(type(body[k]) is not bool for k in DEFAULTS if k!='feed_url'):
            raise ValueError('update_settings_invalid')
        if not isinstance(body['feed_url'],str) or len(body['feed_url'])>600:raise ValueError('update_settings_invalid')
        if body['feed_url']:https_url(body['feed_url'])
        if body['auto_download'] and not body['auto_check']:raise ValueError('update_auto_check_required')
        old=DEFAULTS|self.read('settings.json',{})
        self.write('settings.json',body)
        if old['feed_url']!=body['feed_url']:
            self.write('check.json',{});self.write('pending.json',None)
        return self.status()

    async def check(self):
        cfg=DEFAULTS|self.read('settings.json',{})
        if not cfg['feed_url']:raise ValueError('update_source_not_configured')
        feed=json.loads(await self.fetch(cfg['feed_url'],1024*1024))
        if not isinstance(feed,dict) or feed.get('schema')!=1 or not isinstance(feed.get('releases'),list) or len(feed['releases'])>100:
            raise ValueError('update_feed_invalid')
        system={'Windows':'windows','Darwin':'macos','Linux':'linux'}.get(platform.system())
        arch={'AMD64':'x86_64','arm64':'aarch64'}.get(platform.machine(),platform.machine())
        candidates=[]
        for r in feed['releases']:
            if not isinstance(r,dict) or set(r)!={'version','platform','arch','sha256','commit','url'}:raise ValueError('update_feed_invalid')
            if not all(isinstance(v,str) for v in r.values()):raise ValueError('update_feed_invalid')
            order=version(r['version'])
            if not re.fullmatch('[a-f0-9]{64}',r['sha256']) or not re.fullmatch('[a-f0-9]{40}',r['commit']):raise ValueError('update_feed_invalid')
            url=https_url(urljoin(cfg['feed_url'],r['url']))
            if urlsplit(url).netloc!=urlsplit(cfg['feed_url']).netloc:raise ValueError('update_asset_origin_mismatch')
            if (r['platform'],r['arch'])!=(system,arch) or order<=version(__version__):continue
            if 'rc' in r['version'] and not cfg['include_prerelease']:continue
            candidates.append(r|{'url':url})
        latest=max(candidates,key=lambda r:version(r['version'])) if candidates else None
        self.write('check.json',{'checked_at':time.time(),'release':latest,'feed_url':cfg['feed_url']})
        self.error='';return self.status()

    async def prepare(self):
        if not self.supported():raise ValueError('update_release_installation_required')
        await self.check();check=self.read('check.json',{});release=check.get('release')
        if not release:raise ValueError('update_none_available')
        raw=await self.fetch(release['url'],100*1024*1024)
        if hashlib.sha256(raw).hexdigest()!=release['sha256']:raise ValueError('update_checksum_mismatch')
        path=self.directory/'pending.zip'
        temporary=self.directory/'download.zip';temporary.write_bytes(raw)
        try:await asyncio.to_thread(validate,temporary,release)
        except BaseException:
            temporary.unlink(missing_ok=True);raise
        os.replace(temporary,path)
        self.write('pending.json',{'release':release,'prepared_at':time.time(),'feed_url':check['feed_url']})
        return self.status()

    def install(self):
        if not self.supported():raise ValueError('update_release_installation_required')
        pending=self.read('pending.json',None)
        if not pending:raise ValueError('update_not_prepared')
        if pending['feed_url']!=(DEFAULTS|self.read('settings.json',{}))['feed_url']:raise ValueError('update_source_changed')
        validate(self.directory/'pending.zip',pending['release'])
        state=self.read('operation.json',{})
        if state.get('state') in {'starting','installing'} and time.time()-state.get('at',0)<900:raise ValueError('update_busy')
        self.write('operation.json',{'state':'starting','at':time.time()})
        with (self.directory/'installer.log').open('ab') as log:
            subprocess.Popen([sys.executable,'-m','azusa_bot.updates.worker',str(self.root)],
                cwd=self.root,env={**os.environ,'PYTHONPATH':str(self.root/'src'),'AZUSA_ROOT':str(self.root)},
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        return self.status()

    async def run(self):
        while True:
            await asyncio.sleep(60)
            cfg=DEFAULTS|self.read('settings.json',{})
            if not self.supported() or not cfg['auto_check'] or not cfg['feed_url']:continue
            if time.time()-self.read('check.json',{}).get('checked_at',0)<21600:continue
            try:
                async with self.lock:
                    await self.check()
                    if cfg['auto_download'] and self.read('check.json',{}).get('release'):await self.prepare()
            except Exception:
                self.error='update_check_failed'
                self.write('check.json',{'checked_at':time.time(),'release':None})
