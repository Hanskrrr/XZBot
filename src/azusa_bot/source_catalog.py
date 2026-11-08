"""Reusable public source identities, independent of QQ delivery targets.

The existing rooms table is the source catalog and the single poll snapshot.
No copies of credentials, posts or send cursors cross account boundaries.
"""
from .subscription_sources import TYPES, source_id

MAX_SOURCES = 100


def source_key(row):
    return row['type'] + ':' + str(row['room_id'] if row['type'] == 'bilibili_live' else row['uid'])


def room_id(kind, number):
    return number if kind == 'bilibili_live' else {'bilibili_dynamic':'dynamic','bilibili_video':'video'}.get(kind,kind)+':'+number


def validate_key(key):
    if not isinstance(key,str) or ':' not in key:raise ValueError('subscription_source_invalid')
    kind,number=key.split(':',1)
    if kind not in TYPES:raise ValueError('subscription_type_invalid')
    return kind,source_id(kind,number)


def remember(service, kind, number, *, name='', uid=None):
    if kind not in TYPES:raise ValueError('subscription_type_invalid')
    number=source_id(kind,number)
    if not isinstance(name,str) or len(name)>80 or '\x00' in name:raise ValueError('subscription_source_name_invalid')
    room=room_id(kind,number)
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM rooms WHERE room_id=?',(room,)).fetchone():
            if db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0]>=MAX_SOURCES:raise ValueError('subscription_source_limit')
            db.execute('''INSERT INTO rooms(room_id,uid,name,title,live_status,session,last_check,next_poll,type,payload)
                VALUES (?,?,?,'',0,'',0,0,?,'{}')''',
                (room,uid or ('' if kind=='bilibili_live' else number),name,kind))
    return {'key':kind+':'+number,'type':kind,'source_id':number}


def describe(service):
    usage=service.activity_usage()
    with service.db() as db:
        rows=[dict(r) for r in db.execute('SELECT room_id,uid,name,type,last_check,next_poll,last_error FROM rooms ORDER BY type,room_id')]
        for row in rows:
            row['key']=source_key(row)
            row['source_id']=row['room_id'] if row['type']=='bilibili_live' else row['uid']
            row['push_count']=db.execute('SELECT COUNT(*) FROM subscriptions WHERE room_id=?',(row['room_id'],)).fetchone()[0]
            row['roles']=usage.get(row['key'],[])
            row['polling']=row['key'] in service.activity_sources() or bool(db.execute('SELECT 1 FROM subscriptions WHERE room_id=? AND enabled=1',(row['room_id'],)).fetchone())
    return {'sources':rows}


def remove(service,key):
    kind,number=validate_key(key)
    if service.activity_usage().get(key):raise ValueError('subscription_source_in_use')
    with service.db() as db:
        db.execute('BEGIN IMMEDIATE')
        room=room_id(kind,number)
        if db.execute('SELECT 1 FROM subscriptions WHERE room_id=?',(room,)).fetchone():raise ValueError('subscription_source_in_use')
        db.execute('DELETE FROM rooms WHERE room_id=?',(room,))
    return {'deleted':True}
