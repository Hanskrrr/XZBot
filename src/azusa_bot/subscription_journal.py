"""Account-local, bounded provenance for source reads, role imports and QQ sends."""
import json
import re
from contextlib import nullcontext

from .group_chat import GroupConversation

LIMIT = 10000
RETENTION = 30 * 86400


def clean(value, limit=600):
    text = str(value or '')
    return '[内容含疑似凭据，已隐藏]' if GroupConversation.contains_credentials(text) else text[:limit]


def source_url(kind, number):
    return ({'bilibili_live': 'https://live.bilibili.com/',
             'bilibili_dynamic': 'https://space.bilibili.com/',
             'bilibili_video': 'https://space.bilibili.com/',
             'weibo': 'https://weibo.com/u/',
             'steam': 'https://steamcommunity.com/profiles/'}[kind] + str(number))


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS subscription_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL,
        source_name TEXT NOT NULL, type TEXT NOT NULL, trigger TEXT NOT NULL,
        started_at REAL NOT NULL, completed_at REAL, state TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '', retry_at REAL NOT NULL DEFAULT 0,
        cached INTEGER NOT NULL DEFAULT 0, events TEXT NOT NULL DEFAULT '[]',
        targets TEXT NOT NULL DEFAULT '[]', roles TEXT NOT NULL DEFAULT '[]')''')
    if 'cause' not in {row[1] for row in db.execute('PRAGMA table_info(subscription_runs)')}:
        db.execute("ALTER TABLE subscription_runs ADD COLUMN cause TEXT NOT NULL DEFAULT ''")
    db.execute('CREATE INDEX IF NOT EXISTS subscription_runs_source ON subscription_runs(source_key,id)')


def begin(service, room, trigger):
    from .source_catalog import source_key
    with service.db() as db:
        db.execute('DELETE FROM subscription_runs WHERE started_at<?', (service.clock()-RETENTION,))
        db.execute('DELETE FROM subscription_runs WHERE id IN (SELECT id FROM subscription_runs ORDER BY id DESC LIMIT -1 OFFSET ?)', (LIMIT-1,))
        return db.execute('''INSERT INTO subscription_runs
            (source_key,source_name,type,trigger,started_at,state) VALUES (?,?,?,?,?,'fetching')''',
            (source_key(room),clean(room['name'],80),room['type'],trigger,service.clock())).lastrowid


def finish(service, number, *, info=None, error='', cause='', retry_at=0, cached=False, targets=(), roles=(), state='success', db=None):
    events=[]
    if info:
        for event in info.get('events', [])[-20:]:
            key=str(event.get('session',''))
            url=('https://t.bilibili.com/' if info['type'].startswith('bilibili_') else 'https://m.weibo.cn/detail/')+key
            video=event.get('video') or {}
            if re.fullmatch(r'BV[0-9A-Za-z]{10}', str(video.get('bvid',''))):url='https://www.bilibili.com/video/'+video['bvid']
            events.append({'id':key[:100], 'published_at':event.get('published_at'), 'url':url,
                           'text':clean(event.get('text') or event.get('summary') or video.get('title'))})
        if not events and info['type'] in {'steam','bilibili_live'}:
            events=[{'id':clean(info['session'],120),'text':clean(info.get('title')),
                     'url':source_url(info['type'],info.get('source_id') or (info['room_id'] if info['type']=='bilibili_live' else info['uid'])), 'observed_state':info['live_status']}]
    if not re.fullmatch(r'[a-z0-9_]{0,100}',error):error='subscription_source_error'
    if not re.fullmatch(r'[a-z0-9_]{0,100}',cause):cause='subscription_source_error'
    with (service.db() if db is None else nullcontext(db)) as db:
        db.execute('''UPDATE subscription_runs SET completed_at=?,state=?,error=?,cause=?,retry_at=?,cached=?,
            source_name=CASE WHEN ?='' THEN source_name ELSE ? END,events=?,targets=?,roles=? WHERE id=?''',
            (service.clock(),state,error,cause,retry_at,int(cached),clean((info or {}).get('name'),80),
             clean((info or {}).get('name'),80),json.dumps(events),json.dumps(list(targets)),json.dumps(list(roles)),number))


def list_runs(service, *, source=None, before=None, limit=30):
    from .source_catalog import validate_key
    if source:validate_key(source)
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('subscription_journal_query_invalid')
    if before is not None and (type(before) is not int or before<1):raise ValueError('subscription_journal_query_invalid')
    where,args=[],[]
    if source:where.append('source_key=?');args.append(source)
    if before:where.append('id<?');args.append(before)
    with service.db() as db:
        service._expire(db)
        rows=[dict(r) for r in db.execute('SELECT * FROM subscription_runs'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY id DESC LIMIT ?',(*args,limit+1))]
        more=len(rows)>limit;rows=rows[:limit]
        for row in rows:
            for key in ('events','targets','roles'):row[key]=json.loads(row[key])
            row['source_url']=source_url(row['type'],row['source_key'].split(':',1)[1])
            row['deliveries']=[dict(r) for r in db.execute('''SELECT id,session,kind,target_id,status,
                image_error,created_at,claimed_at,completed_at,text FROM outbox WHERE run_id=? ORDER BY created_at,id''',(row['id'],))]
            for delivery in row['deliveries']:delivery['text']=clean(delivery['text'],1200)
            if row['state']=='fetching' and service.clock()-row['started_at']>180:
                row['state']='interrupted'
    return {'runs':rows,'next_before':rows[-1]['id'] if more else None,'retention_days':30,'max_records':LIMIT}
