"""Bounded source diagnostics retain failures after recovery, without payloads."""
import json
import sqlite3

import httpx

from azusa_bot.subscription_cooldown import SourceCooldown, HEALTH_LIMIT, HEALTH_RETENTION_SECONDS
from azusa_bot.subscription_sources import SubscriptionError
from test_weibo_subscriptions import rig as weibo_rig, body


EMPTY={"last_success_at":0,"last_failure_at":0,"last_failure_code":"","failures_total":0}


def test_old_database_is_upgraded_without_inventing_failure_history(tmp_path):
    path=tmp_path/'data/bilibili.sqlite3';path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE subscription_source_cooldowns (
            source TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '')''')
        db.execute('INSERT INTO subscription_source_cooldowns VALUES (?,?,?,?,?,?)',
            ('weibo:456',2,1900,0,1000,'weibo_login_required'))
    limiter=SourceCooldown(tmp_path,clock=lambda:1100)
    assert limiter.health_many(['weibo:456'])=={'weibo:456':EMPTY}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT retry_at FROM subscription_source_cooldowns').fetchone()[0]==1900
    limiter.success('weibo:456')
    assert limiter.health_many(['weibo:456'])['weibo:456']==EMPTY|{'last_success_at':1100}


def test_recovery_keeps_last_failure_and_total_across_restart(tmp_path):
    now=[1000]
    limiter=SourceCooldown(tmp_path,clock=lambda:now[0])
    limiter.success('weibo:456')
    now[0]=1100;limiter.failed('weibo:456',SubscriptionError('weibo_network_error'))
    now[0]=1200;limiter.failed('weibo:456',SubscriptionError('weibo_login_required'))
    now[0]=2200;limiter.success('weibo:456')
    expected={'last_success_at':2200,'last_failure_at':1200,
              'last_failure_code':'weibo_login_required','failures_total':2}
    restarted=SourceCooldown(tmp_path,clock=lambda:now[0])
    assert restarted.health_many(['weibo:456'])['weibo:456']==expected
    restarted.claim('weibo:456')
    restarted.release('weibo:456')
    assert restarted.health_many(['weibo:456'])['weibo:456']==expected


def test_health_history_has_a_source_cap_and_inactive_retention(tmp_path):
    assert HEALTH_LIMIT==1000 and HEALTH_RETENTION_SECONDS==30*86400
    now=[10000]
    limiter=SourceCooldown(tmp_path,clock=lambda:now[0])
    with sqlite3.connect(limiter.path) as db:
        db.executemany('INSERT INTO subscription_source_health(source,last_success_at,updated_at) VALUES (?,?,?)',
            [(f'weibo:{n}',1000+n,1000+n) for n in range(1002)])
    limiter.success('weibo:new')
    with sqlite3.connect(limiter.path) as db:
        assert db.execute('SELECT COUNT(*) FROM subscription_source_health').fetchone()[0]==1000
        assert not db.execute('SELECT 1 FROM subscription_source_health WHERE source=?',('weibo:0',)).fetchone()
    assert limiter.health_many(['weibo:new'])['weibo:new']['last_success_at']==10000
    now[0]+=HEALTH_RETENTION_SECONDS+1
    assert limiter.health_many(['weibo:new'])['weibo:new']==EMPTY
    SourceCooldown(tmp_path,clock=lambda:now[0])
    with sqlite3.connect(limiter.path) as db:
        assert db.execute('SELECT COUNT(*) FROM subscription_source_health').fetchone()[0]==0


def test_history_keeps_only_fixed_error_codes_and_requested_sources(tmp_path):
    limiter=SourceCooldown(tmp_path,clock=lambda:1000)
    for source in ('weibo:456','weibo:789','bilibili_dynamic:456'):
        limiter.failed(source,SubscriptionError('private response cookie=NEVER_RETAIN'))
    assert set(limiter.health_many(['weibo:456']))=={'weibo:456'}
    assert limiter.health_many(['weibo:456'])['weibo:456']['last_failure_code']=='subscription_source_error'
    with sqlite3.connect(limiter.path) as db:
        records=json.dumps(db.execute('SELECT * FROM subscription_source_health').fetchall())
        records+=json.dumps(db.execute('SELECT * FROM subscription_source_cooldowns').fetchall())
    assert 'NEVER_RETAIN' not in records and 'private response' not in records


async def test_subscriptions_share_health_but_recovery_does_not_erase_failure(weibo_rig):
    service,now,_,_,failure=weibo_rig
    await service.add(body())
    await service.add(body(target_id='20002'))
    before=now[0]
    failure[0]=httpx.Response(432,text='private response')
    now[0]+=301;await service.poll_once()
    failed_at=now[0]
    failed=service.list()
    assert len(failed)==2 and failed[0]['source_health']==failed[1]['source_health']
    assert failed[0]['source_health']=={
        'last_success_at':before,'last_failure_at':failed_at,
        'last_failure_code':'weibo_login_required','failures_total':1}
    failure[0]=None;now[0]+=901;await service.poll_once()
    recovered=service.list()
    assert all(row['last_error']=='' for row in recovered)
    assert recovered[0]['source_health']==recovered[1]['source_health']=={
        'last_success_at':now[0],'last_failure_at':failed_at,
        'last_failure_code':'weibo_login_required','failures_total':1}
