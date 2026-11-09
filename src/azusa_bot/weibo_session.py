"""Explicit mobile-session diagnostics, independent of feed polling.

Only a fingerprint and fixed health metadata are persisted. A successful login
check does not clear source cooldowns or claim that the feed is accessible.
"""
import hashlib

from .subscription_sources import WEIBO_CONFIG_URL, SubscriptionError


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS weibo_session_check (
        id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
        state TEXT NOT NULL, checked_at REAL NOT NULL, error TEXT NOT NULL,
        retry_at REAL NOT NULL)''')
    if 'invalid' not in {r[1] for r in db.execute('PRAGMA table_info(weibo_session_check)')}:
        db.execute('ALTER TABLE weibo_session_check ADD COLUMN invalid INTEGER NOT NULL DEFAULT 0')
        db.execute("UPDATE weibo_session_check SET invalid=1 WHERE state='invalid'")


async def _credential(service):
    values=await service.sources.credentials(('weibo_cookie','weibo_sub'))
    cookie=values.get('weibo_cookie') or ('SUB='+values['weibo_sub'] if values.get('weibo_sub') else '')
    return cookie,hashlib.sha256(cookie.encode()).hexdigest()


def _public(row,fingerprint,configured,now):
    result={'configured':configured,'state':'unknown' if configured else 'unconfigured',
            'checked_at':0,'error':'','retry_at':0,'stale':False,'feed_blocked':False}
    if row:
        result['retry_at']=row['retry_at']
        if configured and row['fingerprint']==fingerprint:
            result.update({k:row[k] for k in ('state','checked_at','error')})
            result['feed_blocked']=bool(row['invalid'])
            result['stale']=now-row['checked_at']>900
            if row['state']=='checking' and row['retry_at']<=now:result['state']='unknown'
    return result


async def status(service):
    cookie,fingerprint=await _credential(service)
    with service.db() as db:
        row=db.execute('SELECT * FROM weibo_session_check WHERE id=1').fetchone()
        sources=[dict(r) for r in db.execute("SELECT uid,name,last_error,next_poll FROM rooms WHERE type='weibo'")]
    health=service.sources.cooldown.health_many('weibo:'+r['uid'] for r in sources)
    for source in sources:source['health']=health['weibo:'+source['uid']]
    return _public(row,fingerprint,bool(cookie),service.clock())|{'sources':sources}


async def verify(service):
    cookie,fingerprint=await _credential(service);now=service.clock()
    if not cookie:return await status(service)
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM weibo_session_check WHERE id=1').fetchone()
        if row and row['retry_at']>now:
            return _public(row,fingerprint,True,now)|{'cached':True}
        invalid=int(bool(row and row['fingerprint']==fingerprint and row['invalid']))
        db.execute("INSERT OR REPLACE INTO weibo_session_check (id,fingerprint,state,checked_at,error,retry_at,invalid) VALUES (1,?,'checking',?,'',?,?)",(fingerprint,now,now+60,invalid))
    state,error,delay='unknown','',60
    try:
        data=await service.sources.request(WEIBO_CONFIG_URL,{},service.sources.weibo_headers(cookie),'weibo')
        login=data.get('data',{}).get('login') if isinstance(data.get('data'),dict) else None
        if login is True:state='valid'
        elif login is False:state='invalid';error='weibo_session_not_logged_in'
        else:state='error';error='weibo_session_response_unknown'
    except SubscriptionError as exc:
        state,error='error',str(exc)
        delay=max(60,min(86400,exc.retry_after_seconds))
    with service.db() as db:
        db.execute('''UPDATE weibo_session_check SET state=?,checked_at=?,error=?,retry_at=?,
                      invalid=CASE WHEN ?='valid' THEN 0 WHEN ?='invalid' THEN 1 ELSE invalid END
                      WHERE id=1 AND fingerprint=? AND checked_at=?''',
                   (state,service.clock(),error,service.clock()+delay,state,state,fingerprint,now))
    return await status(service)


def require_usable_session(sources,cookie):
    """An explicit login:false suppresses feed retries for that exact cookie.

    Replacing credentials or a successful explicit recheck releases this gate;
    network errors/432 alone never count as proof of an invalid login.
    """
    from contextlib import closing
    import sqlite3
    fingerprint=hashlib.sha256(cookie.encode()).hexdigest()
    with closing(sqlite3.connect(sources.cooldown.path,timeout=10)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='weibo_session_check'").fetchone():return
        row=db.execute('SELECT fingerprint,invalid FROM weibo_session_check WHERE id=1').fetchone()
    if cookie and row and row==(fingerprint,1):
        raise SubscriptionError('weibo_session_invalid',retry_at=sources.clock()+3600,retry_after_seconds=3600)
